# CLAUDE.md — project guide for AI sessions

Custom Home Assistant integration controlling the **Pelsall Scout Hut**:
Rointe electric radiators (hall / office / shared kitchen+toilets), a Hyco
Speedflow 15 L water heater, and 3 reversible Vent-Axia ceiling fans on a
Shelly Pro 2PM. The building is a poorly insulated ~20×5 m timber hall
(2.5 m walls, 4 m ridge); the office has loft insulation, the hall does not.
The ceiling fans hang at 11 ft (3.35 m) on standard downrods — ~0.65 m below
the ridge and inside the warm stratified layer, so they are well placed to
reclaim ceiling heat in winter (up-air) and, in summer down-air, drag
apex heat back into the room (which is *why* unoccupied fan-clearing lost
to natural roof venting — open question 9). The ~0.65 m of air above the
blade sweep, right at the apex, is reclaimed by entrainment not direct
sweep, so the ceiling sensor can read hotter than the air the fans reach.

## Working conventions

- **One PR per fix**, squash-merged to `main` with the "(#N)" title
  convention. Develop on `claude/hvac-controller-audit-26ei4w`, restarted
  from `origin/main` each time (`git checkout -B <branch> origin/main`)
  because merges are squashes.
- **Tests are offline**: `pytest` runs without Home Assistant installed
  (`tests/conftest.py` stubs the HA surface; `tests/scout_testkit.py` builds
  a fully wired controller). Every behaviour change ships with tests. 255
  passing as of 2026-07-12.
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
- The Shelly script owns all fan timing/safety (coast-down reversal dwell —
  the blades take ~5 min to stop, so `DWELL_MS` must cover that, not 45 s; stall
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
   **Fan-mixed samples**: cool-offs now carry `fan_ticks`/`ticks` and
   `o1_avg_w` (the 2026-07-11 sealed test measured mixing at roughly *half*
   the stratified gap-normalised loss, so the 11–12 Jul overnight hall samples
   are biased low). Winter recirculation runs the fans through many evening
   cool-offs; if the samples cluster into distinct fans-on/fans-off rates,
   split the constant — otherwise the single EWMA stays (a fan-mixed overnight
   decay may be the *more* honest prediction input on fan-harvesting nights).
   **First winter night (2026-07-13):** the first fully fan-mixed hall
   cool-offs (`fan_ticks`≈`ticks`) landed at ~11.9 %/h, on top of the same
   night's fans-off ~11.7 %/h — *no* distinct clustering, so the single EWMA
   holds for now. The `o1_avg_w` on each sample is what a later split would use
   to separate winter-reverse recirculation from summer-forward mixing.
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
8. **Fan dial stability.** `warmup_sample.o1_avg_w`, `cooloff_sample.o1_avg_w`
   and `fan_change.o1_w` record the transformer tap. If rates cluster by
   wattage band, consider band-aware learning; if the dial never moves,
   speed-blind stays correct. **The tap fingerprint is direction-dependent:**
   summer forward (down-air) draws ~195 W, but the first winter reverse
   (up-air) full-speed draw read ~158 W (2026-07-13) — same dial, different
   motor load. So a wattage band is only comparable *within* a direction;
   `fan_change.direction` / `fan_mode` disambiguate. Confirm the ~158 W winter
   baseline holds over more nights before treating it as the reverse norm.
9. **Sealed-hut fan-clearing test** — **RESOLVED 2026-07-12** (run
   2026-07-11 evening, everything shut, fans forced via calendar event +
   sliders). Gap-normalised bulk (0.75×floor+0.25×ceiling) loss: ~14 %/h
   fan-mixed vs ~26 %/h stratified in the adjacent natural-decay control.
   Mixing pulls the hottest air away from the roof (the best exit) and adds
   ~200 W. Verdict: unoccupied evening fan-clearing is counterproductive;
   summer fans stay occupied-only. Do not automate it; do not "fix" the
   occupancy gate.
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
14. **Pre-heat fan-speed assumption.** Winter pre-heat predicts the hall
    warm-up with the *optimistic* fan-assisted rate (`zone_a_warmup_rate_fans`),
    committing the lead while the Shelly master is off — so a manual dial drop
    is invisible until the fans actually run, and the room can arrive cold.
    `preheat_start.rate_key`/`fan_w_last` and `booking_start.fan_w_last` now
    record which rate drove the lead and the tap the fans were last seen at
    (data-only; the prediction is unchanged). **Decision rule:** if winter
    `booking_start.shortfall` is positive on mornings where `fan_w_last` sits
    in a *lower* band than the **direction-matched** norm (occupants left the
    dial down), the optimistic assumption is the cause — flip the pre-heat to
    predict on the base rate (`zone_a_warmup_rate`, arrive-warm fail-safe). If
    shortfalls do not track a low `fan_w_last`, the fan-assisted rate stays.
    **Compare against the right norm (see Q8):** the winter pre-heat runs the
    fans in *reverse*, whose full-speed draw (~158 W first-seen) is below the
    summer forward ~195 W — so ~158 W is normal here and must not be read as a
    dialled-down fan. The transient
    case (speed changed *during* the idle gap) is unobservable with no
    HA-commandable fan and stays accepted risk. Pairs with Q8: a persistent
    band-aware rate would let the lead size to the *actual* last-seen tap.

## Documented trade-offs (deliberate — re-read the reasoning before "fixing")

- **A breeze-guard stop respects the minimum-run timer.** A 2-second door
  blip grants the vent pass, starts the fans, and its closure leaves up to
  10 minutes of running fans under an active hold (observed in the field,
  2026-07-11 15:41). Kept: bypassing min-run would make drop-off-style door
  traffic flap the fans every minute, and during genuinely busy door periods
  the repeated grants keep fans running exactly when the cross-vent makes
  them useful. The tail is bounded and visible in the audit.
- **A revoked vent pass stays revoked** until every contact closes or the
  latch clears at threshold−1. Opening *more* doors cannot re-grant — the
  contacts are booleans, "more open than before" is invisible. A falling mix
  from the extra opening still releases everything via the latch.
- **Presets re-apply (and re-audit) on every restart** because `applied` is
  deliberately not persisted: re-asserting the hardware state after downtime
  beats suppressing three noise events.
- **The condensation clock resets on restart or a lost reading** — worst
  case is a notification delayed by hours on a days-scale watch.
- **Rejected cool-off folds are audited on every ice→eco transition** — a
  few no-op events per booking day, kept as the evidence the acceptance
  thresholds get tuned from.
- **Sensor-loss fail directions differ by season on purpose**: winter fans
  keep running on loss while demand holds (fail-warm, heat is being made);
  summer fans stop (fail-safe for people).
- **Office eco drift is unjudgeable** (the setpoint lives on the device and
  is never pushed); skipped rather than guessed.

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
