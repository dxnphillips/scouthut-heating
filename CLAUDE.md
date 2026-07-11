# CLAUDE.md — project guide for AI sessions

Custom Home Assistant integration controlling the **Pelsall Scout Hut**:
Rointe electric radiators (hall / office / shared kitchen+toilets), a Hyco
Speedflow 15 L water heater, and 3 reversible Vent-Axia ceiling fans on a
Shelly Pro 2PM. The building is a poorly insulated ~20×5 m timber hall
(2.5 m walls, 4 m ridge); the office has loft insulation, the hall does not.

## Working conventions

- **One PR per fix**, squash-merged to `main` with the "(#N)" title
  convention. Develop on `claude/hvac-controller-audit-26ei4w`, restarted
  from `origin/main` each time (`git checkout -B <branch> origin/main`)
  because merges are squashes.
- **Tests are offline**: `pytest` runs without Home Assistant installed
  (`tests/conftest.py` stubs the HA surface; `tests/scout_testkit.py` builds
  a fully wired controller). Every behaviour change ships with tests. 253
  passing as of 2026-07-11.
- **Data before constants.** This project's core discipline: tuning values
  are changed only against evidence from the audit trail (below), never
  guessed twice. When a value is uncertain, prefer the fail-safe direction
  (heating: err warm; summer fans: err off) and record enough data to decide
  later.
- **The audit trail is the instrument.** `audit.py` keeps a bounded event
  log + a 7-day/15-min readings trace, persisted across restarts, exported
  via the standard diagnostics download (integration page → ⋮ → Download
  diagnostics). The owner pastes that JSON into a session; treat it as the
  ground truth for all tuning decisions. Every learning sample (accepted or
  rejected, with inputs), pre-heat decision, booking start/end outcome,
  preset change (with reason), fan change (with occupied/warm/ΔT/watts),
  seasonal/water/fault event is recorded.
- The Rointe integration is **cloud-based and quirky**: it accepts
  `set_preset_mode` but publishes `preset_mode: null` (drift detection falls
  back to setpoints), exposes a constant nominal "Power" sensor alongside
  the live "Effective power" (discovery prefers effective), and readings can
  freeze while looking alive (hence `last_reported` staleness checks). The
  ceiling Shelly H&T G3 is the opposite: a local threshold reporter (0.5 °C)
  whose silence means "unchanged" — its freshness is availability-only.
- The Shelly script owns all fan timing/safety (45 s reversal dwell, stall
  latch). HA must **never** re-command an unexpectedly-off fan master (that
  re-arms the Shelly's own latch) and only writes the direction relay while
  the master is off.

## Open questions awaiting field data (with pre-agreed decision rules)

Winter 2026/27 — read the first cold-fortnight diagnostics export against:

1. **Winter fan stop threshold (`fan_dt_off` = 0.5).** If the trace shows
   fans running continuously with ΔT plateaued just above 0.5, raise
   `fan_dt_off` to 1.0 (slider first, then default). If ΔT crosses 0.5 and
   fans cycle normally, closed.
2. **Pre-heat cap (default 120 min, slider now to 240).** Judge from
   `booking_start.shortfall` on cold-start mornings: persistent positive
   shortfalls with the lead pinned at cap → raise the slider/default.
3. **Warm-up rates (seeded 60 min/°C, fail-safe).** Expect `warmup_sample`
   events to pull the hall (fans-assisted and base) and office rates toward
   truth over the first booked weeks; `booking_start.shortfall` ≈ 0 is the
   success metric. Target 19.5 °C is now reachable (old 22 never was), so
   completed warm-ups finally exist to learn from.
4. **Gap-normalised heat-loss constants (`zone_X_heatloss_pct`, seed 25).**
   July measurements: hall ~10 %/h, office ~4.5 %/h. Verify autumn/winter
   `cooloff_sample` events (they carry `gap`) confirm season transfer;
   winter wind/infiltration may run k somewhat higher — EWMA absorbs.
5. **Calendar entity mid-event blips.** 2026-07-11 forensics: the calendar
   entity read not-running mid-event once (fans stopped 08:53 BST during a
   06:00–11:00 booking). Watch for `booking_end` + fresh `booking_start`
   pairs mid-slot. Recurring → add an entity-off debounce (treat brief off
   as still-running); once → noise.
6. **Hot-breeze guard calibration (`cooling_mix_max_temp` = 29).** Judge
   `breeze_holdoff` events against felt experience on hot days. Also open:
   whether the hard 35 °C floor cutoff should drop toward 32 (CDC line for
   vulnerable occupants — this is a children's building).
7. **Vent-pass trend test (revoke at mix ≥ best-seen + 0.5).** Calibrated
   from one solar-charge measurement (~+1.8 °C/h with nothing open). Verify
   against real door-open episodes.
8. **Fan dial stability.** `warmup_sample.o1_avg_w` and `fan_change.o1_w`
   record the transformer tap (~195 W at the current setting). If rates
   cluster by wattage band, consider band-aware learning; if the dial never
   moves, speed-blind stays correct.
9. **Sealed-hut fan-clearing test** (pending a hot evening): calendar-event
   recipe in session notes; verdict decides whether evening mixing is ever
   worth automating (expectation: no — the roof is the best exit).
10. **Winter savings forecast**: 500–800 kWh (~£125–215 net) hall heating
    saved by destratification, ±50 %. Verify via degree-day-normalised
    Rointe app statistics vs last winter + duty cycles in the trace.
11. **Condensation watch thresholds** (≥80 % RH below 12 °C for 12 h →
    notify): first winter decides if they're right for this fabric.
12. **Shared-zone spillover**: does hall fan mixing measurably warm the
    kitchen/toilets through open doorways in winter? Compare `shared` in the
    trace across fan-on/fan-off heated sessions.
13. **Drift detection in the field**: setpoint-fallback (Rointe publishes no
    preset) has passed tests but not yet caught a real mid-booking manual
    change. Office-eco remains unjudgeable by design.

**Owner-side outstanding** (not code): set `MIN_RUN_W ≈ 100` in the Shelly
fan script (measured normal draw ~195 W); tag a release after updating;
delete the two orphaned "learned heat-loss rate" entities; possible future
hardware — office split/heat-pump pilot, wall extractor fans, hall window
contacts (they join the vent override automatically when mapped).

## Architecture pointers

- `coordinator.py` — single 30 s reconciler; priority ladder in
  `_desired_zone` (disabled/hold → opening → boost → seasonal lockout →
  alarm → booking/pre-heat → override/motion → empty).
- `preheat.py` — pure optimum-start maths (learned min/°C rates, Newton
  cooling with gap-normalised k). `fan_logic.py` — pure fan decision.
- `audit.py` — event log + trace. `diagnostics.py` — the export.
- `docs/BEHAVIOUR.md` — original-automation → reconciler mapping and all
  behavioural fine print. Keep it and the README in sync with every change.
