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
   fans cycle normally, closed. **2026-07-13:** the first winter run *did*
   plateau (ΔT ~1.8, 17/17 ticks fans-on, empty, no demand) — but the cause
   was warm-fabric ambient stratification in an *empty* hut, not a bad
   threshold, so the fix was the occupancy gate (Q15) rather than raising
   `fan_dt_off`. Re-judge this only against *occupied or heated* plateaus.
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
    saved by destratification, ±50 %. **The saving is delivery-efficiency, not
    loss reduction** — do not conflate them. The fans bring made warmth down to
    head height so occupants reach comfort (and the heaters cut out) *sooner*,
    for less total input; they do **not** slow how fast heat leaves the box.
    So the cool-off "fan-mixed ≈ still" result (Q4/Q9) is *consistent* with a
    real saving, not evidence against it — do not read it as "destrat does
    nothing" and rip the fans out. **Verify the right signal:** heater
    duty-cycle / kWh across *comparable heated, occupied* sessions fans-on vs
    fans-off (degree-day-normalised Rointe stats vs last winter, plus trace
    duty cycles) — NOT cool-off decay, which by construction can't show a
    delivery effect. If the duty-cycle comparison shows no saving either, then
    the destrat thesis genuinely fails and the fans are comfort-only. **Q17 is
    the prior question:** whether there is stratified apex heat to reclaim in the
    first place (capacity vs stratification vs soak-time) — if the 18 °C cap is
    capacity or pure soak-time, there is nothing for destrat to deliver and this
    saving is zero by construction, not by measurement.
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
15. **Winter occupancy gate (`winter_fans_need_occupancy` = on).** The
    no-demand winter recirc path now requires hall occupancy, so an empty,
    unheated hut no longer runs the fans on ambient (warm-fabric)
    stratification — the 2026-07-13 export showed that running as ~150 W of
    continuous cost with, per the cool-off data (fan-mixed loss ≈ still loss),
    no retention benefit. Active heat demand still runs regardless (the savings
    case, incl. pre-heat). The trace now carries `occupied` and `fan_mode`
    alongside `fans`, so empty-building fan-hours are measurable directly.
    **Decision rule:** over the first real cold, occupied weeks, check the
    trace for heated/occupied sessions where destrat clearly helped but the
    gate held the fans off (fans-off while `occupied` false yet a booking was
    imminent / just ended and ΔT was large). If that costs measurable comfort
    or savings, widen the gate to also count *recent* occupancy or an imminent
    booking; if not, the strict gate stays. If deep-winter empty running turns
    out negligible anyway (cold fabric barely stratifies), the gate is
    harmless insurance.
16. **Seasonal lockout threshold (`seasonal_lockout_temp` = 15).** A textbook
    default (UK heating-season base ~15.5 °C on the 3-day forecast mean),
    **not** measured for this building. The July 2026 data cannot validate it:
    the 3-day avg sat at 20–23 °C throughout (never within 5 °C of 15), the hall
    never read below 18.5 °C even at outdoor 12 °C (warm summer fabric coasting),
    and *every* lockout flip was the RealFeel cold-snap release (rf < 13), not
    the average crossing 15 — so **flapping is a cold-snap-clause artifact, not a
    threshold-value problem; do not try to fix flapping by moving 15.** Direction
    of concern is that 15 may be slightly *low* for this leaky, near-zero-gain
    hall: on a cool-but-not-cold autumn day (avg ~15–17) with *cold* fabric the
    lockout stays engaged and a booked session could arrive cold (boost
    overrides, but manually). **Decision rule:** re-judge at the first cool (not
    cold) *booked* autumn session where the 3-day avg falls toward 15–17 — if the
    hall is comfortable without heat, 15 is fine or could go lower; if occupants
    reach for boost / `booking_start.shortfall` is large, raise the slider toward
    16–17 (then the default). The co-heating/UA test would set it analytically
    (the balance-point temperature). Until an autumn export exists, leave it.
17. **Why does the hall cap at ~18 °C — capacity, stratification, or soak-time?
    (The master question under Q10 — is there apex heat to reclaim at all.)**
    Owner reports the hall maxes near 18 °C when heated, and *outdoor-invariantly*
    so (feels the same at −5 °C as at +7 °C). That invariance argues against a
    *steady-state capacity* wall — a loss-vs-capacity balance would sag the max
    on colder days — but it does **not** by itself imply stratification. An
    equally good fit is **never reaching equilibrium within a booking**: a
    cold-fabric *soak-limited* climb whose early rate is ~`Q/C` and so
    ~outdoor-independent, meaning 18 is just where the clock ran out, not a
    thermal ceiling. **Live hint it's the latter:** `zone_a_warmup_rate` is still
    the unlearned 60 min/°C default and **no `warmup_sample` / completed climb to
    19.5 has ever been observed** — every heated episode in the data started warm
    (summer) or never finished. **Caveat the premise:** the "18 max" is old-regime
    memory (setpoint 22, no destrat, pre-integration), not an instrumented
    measurement, and the control has changed. **Three worlds, different fixes:**
    capacity → more kW / envelope (fans ≈ 0); stratification → fans reclaim apex
    heat (the Q10 delivery case, +~1 °C plausible, *not* a qualified figure);
    soak/time → earlier/longer pre-heat (240-min slider), fans help only by
    delivering made heat to head height faster. **Decision rule — read three
    numbers off the first cold, occupied, *heated* export (radiators actually
    working):** (a) ceiling−floor gap *under load* — big (5–6 °C) with the floor
    stuck at 18 → stratification, fans win; small → not; (b) is the floor still
    *rising* at session end or genuinely flat for an hour+ — still rising →
    soak/time-limited, the fix is pre-heat lead, not fans; (c) is the ceiling
    still *climbing* while the floor sits at 18 → yes → heat is being made and
    pooling (stratification). The 15-min trace already carries
    floor/ceiling/demand/occupied/fans, so (a)–(c) need **no code change** to
    read. **A sharper discriminator already exists in HA, also code-free** (the
    Rointe integration is `JYewman/rointe_integration`): each hall heater exposes
    `heating_status` (`idle` / `heating` / `maintaining`, from `status_warming`
    0/2/1 cross-checked against its own probe), its own `current_temperature`
    (the heater's `temp_probe`), and a **real `energy` kWh accumulator**
    (`TOTAL_INCREASING`, so it lands in HA long-term statistics). The
    **probe-vs-floor divergence is the direct capacity/stratification test:**
    heaters pinned at `heating` with their *own* probes still below 19.5 while
    our floor sits at 18 → they cannot lift even local air to setpoint →
    **capacity/loss wall** (fans ≈ 0); heaters dropping to `maintaining`/`idle`
    (probes satisfied at 19.5) while our floor still reads 18 → heat reaches the
    probes but not the far field/floor → **stratification** (fans win); floor
    still climbing → **soak** (the fix is pre-heat lead). Q10's delivery signal
    is the same `energy` kWh, degree-day-normalised, fans-on vs fans-off —
    readable from HA statistics with no trace change. **Caveat on the power
    sensor:** "Effective Power" reads 0 whenever the device is idle or at target
    and is *modelled* at 100/50 % of nominal when the device reports no real
    `effective_power`, so treat `heating_status` + the `energy` accumulator as
    the trustworthy duty/saturation signals, not effective-power as a wattmeter.
    Pairs with Q3 (warm-up learning) and Q10 (this is the prior question Q10's
    saving depends on). The co-heating/UA test (Q16) would settle capacity
    analytically.

- **The hall pause is manual-resume, no timer, hall-only — on purpose.** The
  Rointes are child-locked, so `hall_heating_paused` (the *Pause hall heating*
  button) is the only occupant-accessible way to stop the heat. It forces the
  hall to ice above boost/booking but still frost-protects, and holds the
  *winter* fans off (they'd deliver roof heat onto the too-warm person) while
  leaving the *summer* breeze running. Deliberately **no timer** (owner
  preference): it clears only on Resume, a hall boost (the two are mutually
  exclusive), a hall pre-heat window opening from an idle gap, or a hall
  `booking_end`. The idle-gap rule rides the `cal_window` false→true edge, which
  *cannot* fire mid-booking — so **adjacent bookings** don't resume on the
  current too-warm occupants; the pause lifts at the running booking's end and
  the next session inherits the warm room with a shortened/absent pre-heat
  (owner-confirmed as the wanted behaviour). Frost protection means a forgotten
  pause can't freeze the hut, only leave a later group cool until a button /
  the next-session clear wakes it.
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
- **Active hall heating forces the reverse/destrat regime, even under summer
  lockout.** Forward = down-air = wind-chill, so blowing it on a hall that is
  being *heated* (a boost or booking sets comfort/eco) would chill the people
  the heat is for. `_fan_target` sets `heating = applied[ZONE_A] in
  (comfort, eco)` and passes it to `fan_decision`, which runs the reverse
  (up-air) branch whenever heating is active. Keyed off the *preset*, not
  `demand`, so the direction can't flap as the radiator thermostat cycles — the
  cost is up to two reversals per *summer* boost (start + end), which is rare
  and accepted. Winter is unchanged (already reverse). The winter run/stop
  rules still apply, so heating a room that's already warm (no demand, floor
  above the recirc cap) just leaves the fans off rather than blowing anything.
- **Office eco drift is unjudgeable** (the setpoint lives on the device and
  is never pushed); skipped rather than guessed.

**Owner-side outstanding** (not code): set `MIN_RUN_W ≈ 20` in the Shelly
fan script — the stall threshold must sit *below the lowest running draw*, and
the **2026-07-14 commissioning measured the lowest forward dial at 40 W** (full
forward ~195–255 W depending on tap; reverse ~0.6–0.8× forward, so its lowest
tap is ~25–30 W). An earlier `≈ 100` guess (sized off the ~195 W *normal* draw)
was wrong — it would latch a false stall on any low-dial running; ~20 W clears
the 40 W forward floor and the lower reverse floor while staying above the
~0 W closed-master draw, and matches the HA-side `FAN_RUNNING_MIN_WATTS = 20`
(kept just below the Shelly threshold). Measure the lowest *reverse* draw to
pin it exactly. **Also raise `STALL_W` 260 → ~350**: the 260 placeholder sits
only 5 W above the measured 255 W high-forward tap, inside the Shelly's own
power-reading noise, so normal high-dial running trips a false stall (a likely
cause of the 2026-07-14 commissioning fault-latching). ~350 clears 255 W with
margin yet stays far below a locked-rotor draw; measure a real stall to refine.
The reference script (`docs/reference/fan_reverse_supervised.js`) already
carries these corrected values. Tag a release after updating;
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
