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
   **The latent risk is now arming (2026-07-22 → 30, data).** Through summer
   every booking arrived warm (shortfalls all *negative*, room above target on
   the warm fabric), so this never fired. But the hall has since been seen
   genuinely cold-starting — floor to 16.4 °C at outdoor 7.5 °C, and 17.25 °C on
   the cool 27 Jul morning — the first real deficits. With the warm-up rate
   still the unlearned 60 min/°C seed (Q3/Q17: no completed climb ever observed)
   and the cap still live at **120**, a cold-start of ~2.5 °C needs ~150 min and
   is clamped to 120 → the first cold *booked* morning will likely arrive short.
   The `OUTDOOR_MARGIN` multiplier scales the *needed* lead up on cold mornings
   *before* the clamp, so the cap binds harder, not softer. **Pre-emption
   (fail-safe, no code):** raise the `preheat_minutes` slider toward ~180 before
   the first cold booking rather than waiting to read the shortfall — earlier
   start only sits the room at comfort a little early, it cannot overheat past
   setpoint. Then still watch the first cold booked morning's shortfall to
   confirm and to size the learned rate.
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
   **Cool-off min-gap raised 3.0 → 4.0 (2026-07-18, code review + data).** The
   spikes the shoulder-season exports kept showing (rate jumping to ~20 %/h then
   recovering) were traced to short *just-vacated-room* samples at gap 3.35–3.40
   shedding stored heat fast into a cool evening: across 36 accepted hall
   cool-offs, every spike came from gap < 4, every *reliable* sample from
   gap ≥ 4. Below ~4 the `k = drop/(hours·gap)` normalisation is too noisy to
   trust, so `MIN_COOL_SAMPLE_GAP` now rejects them. Fail-safe-neutral: it drops
   noise, it does not clamp a genuine high-loss reading *down* (which would
   shorten the lead and risk a cold arrival), so `MAX_COOL_K` stays 0.5.
   **Single-tick step guard added (`MAX_COOL_TICK_DROP` = 1.5, 2026-07-22, data
   + owner insight).** The *office* (zone_b) EWMA was found yanked 4.7 → 24 %/h
   — physically absurd (higher than the *uninsulated hall's* 8.8), traced to two
   single-tick reading steps (24.5 → 21.5 and 20.5 → 19.0, each a whole 1.5–3 °C
   drop in one ~30 s reconcile). Cause: **open office windows/doors** with **no
   office contact sensor mapped** (only `zone_a_doors` + `shared_windows` exist),
   so the `opening_ice` guard — which correctly discards the *hall's* open-door
   cool-offs — is structurally blind to the office; the ventilation drop was
   learned as fabric loss. These slipped the gap floor (office gap is genuinely
   ≥ 4) but are noise on the *step* axis. A genuine fabric cool-off eases the
   reading down one 0.5 °C quantum at a time; a lone tick shedding ≥ 1.5 °C is a
   discontinuity (opening, or the Rointe probe unfreezing and dumping an
   unknown-duration drop into one reading) whose per-tick *rate* is
   uninterpretable, so the whole sample is now rejected. Set above plausible
   quantised cooling (one 0.5 °C quantum/tick) to protect genuine fast winter
   cool-offs; fail-safe-neutral (drops noise, never clamps a real reading down).
   `cooloff_sample.max_tick_drop` now records the largest single-tick fall.
   **Two real fixes still outstanding for the office:** (a) owner is fitting
   office door/window contacts — once mapped they feed the opening guard exactly
   as the hall's do (the proper structural fix); (b) the corrupted
   `zone_b_heatloss_pct` was manually reset to ~5 %/h (the EWMA does not
   self-heal fast). **If winter shows the guard starving genuine fast hall
   cool-offs** (real losses presenting as steps through a coarse-reporting
   probe), raise the threshold — but each such sample has a meaningless rate
   anyway, so rejecting is the correct default.
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
   **First real reverse-run taps (2026-08-05 field data, both from the
   cold-booking pierce / evening resume — see Q10).** The ~158 W baseline held on
   the *morning* run (`fan_change.o1_w` 158–170 W, reverse), but the *evening*
   run read **~58 W** reverse — the same day, a much lower tap. So **the dial
   demonstrably moves** (158 → 58 W within one day, both reverse), which is the
   condition that makes speed-blind learning wrong: a wattage band *is* worth
   tracking here, at least within a direction. Forward the same day sat at its
   usual ~200–210 W. The 58 W evening tap still moved the ceiling strongly (Q10),
   so low-tap reverse is not "barely running" — the stall/`MIN_RUN_W` thresholds
   must sit well below it (owner-side note already flags ~20 W).
9. **Sealed-hut fan-clearing test** — **RESOLVED 2026-07-12** (run
   2026-07-11 evening, everything shut, fans forced via calendar event +
   sliders). Gap-normalised bulk (0.75×floor+0.25×ceiling) loss: ~14 %/h
   fan-mixed vs ~26 %/h stratified in the adjacent natural-decay control.
   Mixing pulls the hottest air away from the roof (the best exit) and adds
   ~200 W. Verdict: unoccupied evening fan-clearing is counterproductive;
   summer fans stay occupied-only. Do not automate it; do not "fix" the
   occupancy gate. **The same 26→14 %/h number is the WINTER retention benefit
   with its sign flipped** (2026-07-18): in summer, retaining heat by pulling it
   off the roof is *bad* (you wanted to vent it); in winter, that halved loss
   rate is exactly the roof-loss reduction destrat is meant to give — see Q10.
   **First *field* (non-sealed) reverse runs, 2026-08-05** (the fans' first
   winter-mode use in the audit — every prior run since 07-22 was summer/forward).
   The evening run reproduced the sealed test's mechanism in real winter-ish
   conditions (dark, 14.9 °C out, occupied): the fans stripped the hot ceiling
   down ~4× faster than passive cooling, gap 2.58 → 1.68 in 41 min — "pulling the
   hottest air away from the roof," now measured outside the sealed rig. Full
   numbers and the morning solar-washout counter-case are under Q10.
10. **Winter savings forecast**: 500–800 kWh (~£125–215 net) hall heating
    saved by destratification, ±50 %. **The saving has TWO components — delivery
    efficiency AND loss reduction — both stratification-dependent (2026-07-18
    correction, owner insight).** (a) *Delivery:* the fans bring made warmth down
    to head height so occupants reach comfort (and the heaters cut out) *sooner*,
    for less input. (b) *Retention:* a hot stratified roof is the biggest,
    leakiest loss surface in this bare hall, so removing that hot layer cuts the
    loss rate — **Q9's sealed test measured exactly this: ~26 %/h stratified →
    ~14 %/h fan-mixed, nearly halved, "by pulling the hottest air away from the
    roof."** Both components reduce heater duty. **Earlier "delivery not loss
    reduction" wording was wrong** — it over-generalised Q4's *overnight*
    "fan-mixed ≈ still" cool-offs, which were near-uniform (ΔT 1.5–2, no hot roof
    to remove) so of course mixing changed nothing there. Retention is nil when
    the column is uniform (Q4) and substantial when a hot roof exists (Q9) — and
    a heated, occupied winter hall is the hot-roof case. So do not read the
    weakly-stratified cool-off "≈ still" as "destrat does nothing"; it only means
    "nothing to destratify on that night." **Caveat:** Q9's 26→14 is one *summer
    sealed* test — winter needs confirming, and both components still require the
    heaters to build a hot roof (Q17 — capacity-limited = no hot roof = neither
    effect). **Verify the right signal:** heater
    duty-cycle / kWh across *comparable heated, occupied* sessions fans-on vs
    fans-off (degree-day-normalised Rointe stats vs last winter, plus trace
    duty cycles) — NOT cool-off decay, which by construction can't show a
    delivery effect. If the duty-cycle comparison shows no saving either, then
    the destrat thesis genuinely fails and the fans are comfort-only. **Q17 is
    the prior question:** whether there is stratified apex heat to reclaim in the
    first place (capacity vs stratification vs soak-time) — if the 18 °C cap is
    capacity or pure soak-time, there is nothing for destrat to deliver and this
    saving is zero by construction, not by measurement.
    **First field destrat data (2026-08-05, the two first-ever winter-mode fan
    runs — both from the cold-booking lockout pierce and the evening resume).
    Mechanism confirmed; kWh saving still unmeasured.** Two runs, opposite
    conditions, opposite results — the contrast is the finding:
    - *Morning (non-diagnostic — solar washout).* Warm sunny morning, weak
      stratification (gap ~1.8), occupants arriving, reverse tap ~158 W. The gap
      ended *wider* than it started (1.78 → 2.02) because the sun loaded the
      uninsulated roof faster than the low tap could mix it down; after the fans
      stopped it re-stratified hard (gap → 2.6). Confounded by occupancy (kids
      arrived exactly as the fans started) and heaters barely firing. Reads as
      "destrat does nothing" but only because there was nothing to reclaim and the
      roof was being re-charged — exactly the wrong-conditions case Q4/Q9 predict.
    - *Evening (clean measurement, with a same-evening natural-decay control).*
      Dark, 14.9 °C out, occupied, **heaters off the whole time** (demand False —
      pure fan redistribution, no input), reverse tap **~58 W**. A real hot-ceiling
      reservoir had built naturally after the summer fans stopped (gap 1.9 → 2.58
      over the evening). Then 41 min of reverse fans: **ceiling 22.7 → 21.8
      (−0.9 °C), gap 2.58 → 1.68 (nearly halved), floor held flat at 20.12.** The
      gap closure decelerated (−0.4/−0.3/−0.2 per 15 min) exactly as a mixing rate
      ∝ gap predicts. **Control:** in the fans-off decay earlier that evening the
      ceiling cooled ~0.08 °C/15 min; under the fans ~0.3 °C/15 min — **~4× faster**,
      so it is the fans stripping the roof, not ambient loss. The floor, which had
      been *falling* ~0.08 °C/15 min in decay, **stopped falling** once the fans ran.
    **What it settles and what it doesn't.** It splits cleanly along Q10's two
    components: **retention is now demonstrated** (ceiling pulled down 4× the
    passive rate — Q9's "hottest air off the roof," measured in winter-ish
    conditions outside the sealed rig for the first time); **delivery shows only as
    *arrested cooling*** (floor held rather than rose) — as expected with no heater
    input, there is no *new* heat to deliver, only redistribution that offsets
    loss. What it still cannot show is the **kWh / duty-cycle saving** — the
    heaters never fired, so the Q10 signal (degree-day-normalised heater duty,
    fans-on vs fans-off in a *heated* session) is untouched; this is the mechanism,
    not the money. Caveat: one evening, one 41-min window. But it is the first
    field evidence that destrat works when the roof heat is yours to reclaim and
    nothing re-charges it (a cold dark evening) and does nothing when the sun is
    loading the roof faster than the fans clear it (the morning) — a sharp line
    for when the winter fans earn their ~58–158 W.
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
    **Open sub-question (2026-07-18 code review, no data yet): the gate has a
    demand-side leak.** `worth_moving = demand or recirc`, but the occupancy
    gate only guards `recirc`; `demand` (`_heat_demand`) is *building-wide* on
    purpose (it catches an office/shared heater warming the leaky hall). So an
    **office-only** heat demand runs the **hall** fans in an *empty* hall,
    exactly the empty-running Q15 was added to stop, leaking through the demand
    path. Unobservable so far — there has been *no* heat demand at all this
    summer (every winter/reverse run in the audit had `demand=False`), so it
    has never fired. It is also only a *waste* if Q12 (does office/shared heat
    measurably stratify the hall?) comes back *no*; if office heat does warm the
    hall, destratifying it is arguably the delivery case even with the hall
    empty of people (though there is then nobody to deliver comfort to).
    **Decision rule:** in the first real winter with office-heated / hall-empty
    sessions, check the trace for reverse fans running while `occupied` false
    and only an office/shared heater is on. If it is pure cost (Q12 = no
    spillover), make the hall-fan `demand` hall-specific; if Q12 shows real
    spillover, leave it. Do not "fix" it blind — it rides intended design and
    the unverified Q12.
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
    **First field instance of the concern (2026-07-27, not yet a booked
    session):** a genuinely cool morning — RealFeel 13.8 °C, outdoor 17.6 °C,
    hall floor ~17.9 °C — but the 3-day average sat at **19.05**, so the lockout
    stayed *engaged*. Confirms the mechanism the decision rule anticipates: the
    average lags the felt cold, so a booked session on a day like this gets **no
    automatic heat** (booking/motion sit below the lockout; only a manual boost
    pierces it — and boost *does* work here because the hall is genuinely below
    the 19.5 setpoint). Not yet actionable (no booking coincided, and the summer
    average genuinely *is* warm, so 15 is not "wrong"), but it is the first real
    data point — re-judge when this recurs on a cool *booked* autumn morning with
    boost use / a positive shortfall. **Note the felt-cold here is also part
    operative-temperature, not just air** — see Q19.
    **Open sub-question (2026-07-18 code review, no data yet): does the flapping
    drive fan-*direction* flapping?** `_summer_active` follows the lockout, so a
    flip flips the fans' wanted direction (summer forward ↔ winter reverse) —
    and if the hall is simultaneously warm, occupied and stratified, each flip
    would command a full ~5-min reversal (and enough of them inside
    `MAX_REVERSE_ATTEMPTS` could latch a `reverse_failed` fault). There is **no
    debounce** on the season→direction coupling. But the data does *not* show it
    happening: across the week only 4 direction changes occurred, each a genuine
    half-day/full-day seasonal transition (well spaced), never rapid flapping,
    and never with the warm+occupied+stratified combo. **Decision rule:** watch
    the first real shoulder-season for *multiple* reversals within a few hours
    (rapid direction flips in `fan_change.direction`) coincident with lockout
    flips on an occupied warm day. If seen → debounce the direction change (hold
    the last direction until the season has been stable for N minutes). If the
    only direction changes remain genuine day-scale transitions, the coupling is
    harmless and no debounce is needed.
    **Superseded for anyone running `fans_follow_state` (F6, 2026-08-06):** with
    that switch on the fan direction no longer follows the season at all — it
    follows the hall's thermal state (`_fan_cooling_regime`), so a lockout flip
    cannot flap the direction and this sub-question is moot. It remains live only
    for the default (season-derived) mode; a debounce would only ever be needed
    there.
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
    **Does weather compensation ever become useful here? (2026-07-18, from a
    review of `smartthings54/smart-climate-control`, corrected after owner
    pushback.)** The setpoint is *ours* — we push `hall_comfort_temp` to the
    Rointe comfort number — so "we can't modulate" was the wrong reason to
    dismiss it. The Rointes are **not** pure on/off either: they throttle to
    `maintaining` (~half power, `status_warming` 1) *below* target to avoid
    overshoot, vs `heating` (full, status_warming 2). So **raising the setpoint
    on a cold day can flip a throttled heater back to full power — a real
    setpoint-based weather compensation.** Whether it *helps* is regime-dependent
    and answered by the SAME Q17 winter `heating_status` data:
    (a) heater in `maintaining` while the room is still short → raising the
    setpoint restores full power → **benefit**; (b) heater pinned at `heating`
    and *still* short (capacity wall) → **does nothing**, it is already maxed;
    (c) room reaches 19.5 fine → raising it just **overheats** (wrong tool). Two
    caveats even in case (a): more input ≠ delivery to the floor (if the limit
    is stratification, the extra heat pools at the ceiling and the *fans* are the
    better delivery path — setpoint-raise and fans are complementary, not
    rivals), and any weather-comp setpoint must be an **offset on top of** the
    19.5 slider, never an overwrite of the owner's setting. The lower-risk
    outdoor-driven setpoint move, useful regardless of regime if Q17 is
    **soak/time-limited**, is **fabric pre-charging** — hold a higher overnight
    eco floor on a forecast-cold night before a morning booking so the cold-start
    soak is already underway (this is the same anticipatory-pre-heat idea; the
    lead is *already* outdoor-sized from `zone_a_heatloss_pct` + Newton cooling
    and assumes a cold 5 °C when outdoor is unreadable). **Decision rule:** read
    the first cold, occupied, heated export's per-heater `heating_status`; only if
    heaters are throttling to `maintaining` while the floor is short is a
    setpoint weather-comp worth building, and even then weigh it against the fans
    for stratification. *Classic* modulating weather compensation (continuous
    output vs outdoor) still only applies to a genuinely modulating device — the
    office heat-pump/split on the future-hardware list — at which point
    `smartthings54/smart-climate-control` is worth revisiting for that device.
18. **Surface a sustained heater outage (candidate feature, not yet built —
    2026-07-30, field incident).** The whole Rointe integration (all four hall
    heaters + office) went `unavailable` at 29 Jul 07:16 and stayed down ~27 h;
    a manual integration reload fixed it (a stale cloud session, not lost power —
    so the devices frost-protected locally throughout). **Nothing surfaced it:**
    no persistent notification, no HA repair issue, no heater-specific audit
    event — the only trace was a `fan_sensor_lost` event (a *side effect* of
    losing the floor probe, which breaks the fan mix). By design the coordinator
    tolerates offline heaters silently (drift skips them, presets re-apply on
    reconnect — correct for a brief cloud blip, which this integration does
    routinely: `fan_sensor_lost` fired 3× in two weeks, 15/28/29 Jul, i.e. a
    flaky link). But a *sustained* loss of **all** heaters is different in kind:
    harmless under summer lockout, yet in winter it is a silent blind spot — a
    cold snap or a booking arriving with no heat and no warning, and no frost
    protection at all if the devices lost power rather than just cloud. The
    audit trail is the instrument, and a 27 h total outage left no mark on it.
    **Decision rule / design:** add a duration-gated alert — if every mapped
    heater in a zone (or all zones) reads `unavailable`/`unknown` for more than
    N reconciles (a few minutes, so a normal cloud hiccup does not cry wolf),
    raise a persistent notification **and** an audit event (`heaters_offline`
    with zone + duration), dismissed on recovery — mirroring the fan-fault and
    opening notifications. Gate N above the observed routine-blip length. Low
    urgency now (summer); do before the first heating season. Pairs with the
    Rointe staleness checks (`last_reported`) already in place for the
    freeze-while-alive case — this covers the distinct *unavailable* case.
19. **Is 19.5 the right comfort target for a low-activity seated group? (No
    data / no measurement yet — 2026-07-27 discussion.)** `hall_comfort_temp`
    = 19.5 is an *air-temperature* setpoint, but what a still, seated group feels
    is *operative* temperature ≈ the average of air and the mean-radiant
    temperature of the surrounding surfaces. In this bare, uninsulated hall the
    walls and roof run cold, so operative temperature sits a degree or two
    *below* the air reading — worst-case for a sedentary meeting (low metabolic
    heat, long exposure, feet near a cold floor). Two compounding blind spots:
    (a) the "floor" reading is the Rointe **mid-wall probe**, not seated-height
    air, so it can *overstate* comfort where people actually sit; (b) we have
    **no radiant/surface-temperature sensor and no seated-height air sensor**, so
    "it felt cold at 19.5" is currently unfalsifiable from the trace. **Decision
    rule:** on cool *booked* sedentary sessions, watch for boost use and
    `booking_start.shortfall`; if occupants reliably reach for more heat at a
    satisfied 19.5, the fix is either a higher hall comfort setpoint or an
    *activity-aware* target (a warmer number for low-activity bookings), **not**
    a code change to the control law. The measurement gap is the deeper issue:
    if this recurs, a cheap surface/globe-ish sensor or a seated-height air
    sensor would turn the felt complaint into data. Reverse (destrat) fans help
    only once the heaters have built a warm roof to bring down — on an unheated
    cool day there is nothing to deliver, and forward fans would chill. Pairs
    with Q16 (the lockout can leave such a session unheated) and Q17 (delivery
    vs capacity vs soak).
20. **Drive self-validation — does the loop know its commands are working?
    (Candidate feature, backlogged 2026-08-06, owner request.)** The drive-to-
    target loop notices when the *room* doesn't respond (staircase climbs, then
    the `drive_capped` alert at the cap) — but it does NOT notice when its own
    *command* isn't landing. The v1.14.0–v1.14.2 phantom-push bug (writing the
    comfort number without re-applying the preset) was invisible to the loop:
    "pushed 22.5, probe didn't rise" looked identical to a capacity wall, and it
    would have blamed capacity. Two validations to add (both use signals the
    Rointe cloud can't fake): (a) **setpoint read-back** — after pushing X, check
    the heater's *reported* `setpoint` (now in diagnostics, v1.14.4) matches X
    within a settle window sized to the real cloud lag; a persistent divergence
    is "the heater isn't accepting our setpoint," a distinct fault from "can't
    reach target." (b) **independent ceiling cross-check** — the roof thermometer
    is an independent witness; if the drive is boosting hard with `demand` on but
    neither the floor probes NOR the ceiling move over a long window, flag "heat
    requested, no response anywhere." **Build only after the drive is confirmed
    to actually move the hardware** and the read-back settle window can be tuned
    against measured lag (not guessed — guessing it is how a false-alarm bug gets
    born). Diagnostics now carry each heater's `setpoint` + `action` (v1.14.4) as
    the foundation.
    **BUILT 2026-08-06 (`drive_self_check`, default ON, notification-only).** Both
    checks shipped in `_reconcile_drive`. (a) *Setpoint read-back*
    (`_check_setpoint_readback`): after a push has had `DRIVE_SETTLE_MINUTES` (10)
    to round-trip, the heater's reported `temperature` must be within
    `DRIVE_SETPOINT_TOL` (0.3) of what we pushed; a settled divergence adds the
    heater to `_drive_rejected` → `drive_setpoint_rejected` audit + persistent
    notification (`NOTIFY_DRIVE_REJECTED`). The settle window is deliberately
    generous (≫ the real seconds-to-a-minute cloud lag) so ordinary lag cannot
    false-alarm — the "not guessed" mitigation the caution asked for, achieved by
    over-sizing rather than by measuring. Abstains (drops the heater) before the
    window elapses or when the setpoint is unreadable. (b) *Independent ceiling
    cross-check* (`_update_drive_no_response`): while the hall is in comfort and
    its coldest probe is short of target, if over `DRIVE_NO_RESPONSE_MINUTES` (45)
    NEITHER the floor NOR the ceiling rises by `DRIVE_NO_RESPONSE_EPS` (0.3), raise
    `drive_no_response` + `NOTIFY_DRIVE_NO_RESPONSE`. Deliberately NOT gated on
    `demand` (a dead chain reads 0 W, so demand-on would miss it); the ceiling is
    the discriminator from a capacity wall, which still warms the ceiling by
    stratification (so a rising ceiling resets the window). Needs both floor and
    ceiling readable or it abstains (no independent witness). Diagnostics gained
    `drive.self_check` / `setpoint_rejected` / `rejected_alert` / `no_response_alert`.
    **First-winter watch:** confirm neither alert false-fires on a normal cold
    climb (the settle/window sizes are still first guesses — over-generous on
    purpose); if the read-back ever flags on genuine slow adoption, widen
    `DRIVE_SETTLE_MINUTES` rather than tightening the tolerance.

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
- **Fan direction can follow room state instead of the season
  (`fans_follow_state` = off, F6).** By default the cooling-vs-destrat *default*
  direction is season-derived (`_summer_active`: manual force, or the seasonal
  lockout via `summer_follows_season`) — a 3-day-average outdoor crossing flips
  it. With this switch on, `_fan_cooling_regime(warm, heating)` sets the direction
  from the hall's *thermal state*: cool (forward) only when the head-height air is
  genuinely warm (`warm`, i.e. above `cooling_temp_high`) **and** the hall is not
  being heated; destratify (reverse) otherwise. So the direction tracks the
  thermometer, not the calendar — a warm hall gets a breeze even in "winter"
  season, a cool hall destratifies even in "summer" season, and a lockout flip
  can no longer flap the direction (the Q16 sub-concern, mooted by this). Manual
  `summer_mode` still forces cooling; active heating still forces reverse (the
  `heating` gate above is unchanged and wins). A warm reading is *required*, so no
  floor / unknown warmth never blows a cooling draught on assumption — the same
  fail-safe the summer branch keeps. Only the `summer` argument into
  `fan_decision` changes; the run/stop thresholds, the overheat/breeze guards and
  the hall-pause early-out are untouched (the pause early-out still reads
  `_summer_active`, so a paused-and-warm hall in *winter season* under state-mode
  is the one unhandled corner — negligible, noted not fixed). Default OFF — the
  season-labelled default stands until the owner opts in. **First-shoulder-season
  watch:** confirm the direction now only changes on real warm↔cool state
  transitions (in `fan_change.direction`), not on lockout flips, and that a warm
  winter hall getting a forward breeze is actually wanted (if not, the fix is a
  higher `cooling_temp_high`, not re-coupling to the season).
- **The heaters are driven to target, not trusted (`drive_to_target` = on).**
  The Rointes settle a fraction *below* the setpoint we give them (field
  2026-08-06: a hall probe held ~0.5 below a 19.5 comfort setpoint on a cold
  night while reporting a *modelled* "full" 1800 W the radiators didn't match —
  effective-power is not a wattmeter, per Q17). Since the integration owns the
  setpoint it pushes, `_reconcile_drive` closes an outer loop on each heater's
  **own probe** and overdrives the setpoint until the probe actually reaches
  target. **Per-heater** (each of the 20 m hall's ends independently), across
  **all three zones** (hall/office/shared — office & shared setpoints are now
  *owned* by the integration via `office_comfort_temp` / `shared_comfort_temp`
  sliders, a deliberate reversal of the old device-managed design; it also gives
  the office the setpoint sliders it never had). The controller (`drive.py`) is a
  **staircase integral** — nudge one 0.5 step, wait `STEP_INTERVAL_MIN` (15) for
  the slow building to respond, re-check — *not* a PID: the feedback is 0.5-
  quantised (no derivative) and the plant is slow and model-free (a continuous
  integral winds up and overshoots). The wait between steps IS the anti-windup.
  A heat-loss **feedforward** gives a cold-night head-start, capped at one step so
  it can't overshoot a non-drooping heater. **A Rointe only adopts a changed
  comfort *number* when the comfort *preset* is re-applied, so each drive push is
  followed by a per-heater `set_preset_mode` re-assert** — without it the boost
  writes the number but never reaches the radiator (v1.14.2 shipped that no-op;
  v1.14.3 fixed it: the heaters sat at the last-applied setpoint while the drive
  logged phantom pushes). The existing slider-change path
  (`async_hall_temps_changed`) always did push-number-then-re-apply for the same
  reason. It only ever drives *harder* than the
  owner's setpoint; clamped to `[target, target + drive_max_offset]` (default
  offset 4.5 → hall cap 24) and the 30 Rointe max. **Safety net:** stale/glitched
  probe withdraws that heater to the plain target (fail-safe); cross-probe sanity
  (a probe > 4 below the zone median is distrusted); **last-will reset** on
  unload AND on startup (a crash can't leave an overdrive — the staircase is not
  persisted); a `drive_capped` audit + persistent alert if a heater sits pinned
  at the cap while still short for `DRIVE_CAP_ALARM_MINUTES` (60) — a real
  capacity wall (Q17) or a stuck sensor. Hands off a zone under manual-hold /
  automation-off. While driving a comfort zone, drift detection **skips the
  setpoint comparison** (the setpoint is ours and the Rointe cloud lags our
  pushes, so comparing it false-flagged a manual hold on the v1.14.0 startup —
  fixed in v1.14.1; and a persisted such hold, which deadlocked because it kept
  `expected_preset` at None and so never reached the clear path, now clears on
  the next tick when driving is on — v1.14.2); a manual *preset* change is still
  caught, and disabling automation is the way to take manual control. **This also settles Q17 by experiment:**
  if driving to the cap reaches target it was throttling (fixed); if it pins at
  the cap still short, that's the definitive capacity wall (fans ≈ 0, needs kW).
  **First-winter watch:** confirm it lands *on* target without hover/overshoot on
  the slow fabric, and read `drive_capped` / the `drive.pushed` trace for which
  heaters need the most boost.
- **A cold booking pierces the seasonal lockout (`cold_booking_heats` = on).**
  The summer lockout freezes heating, but a booked (or pre-heating) session
  whose room is genuinely below the target it is asking for still heats — an
  out-of-season cold snap on a booked day must not be frozen out.
  `_cold_booking_bypass` gates the `seasonal_lockout` rung in `_desired_zone`:
  switch on, `_cal_active` (so the pre-heat window is covered — a cold booked
  morning is warm from minute one), and the room's **coldest** heater probe
  below the booking target (`_booking_target` — comfort, or eco-low for an
  ECO-keyword event). **Self-calibrating, no weather constant** (deliberately
  *not* the outdoor "cold-snap" framing the owner first reached for): a
  warm-fabric summer booking already at target does not bypass, so this stays a
  cold-snap escape hatch, not a season-long defeat of the lockout. Release
  hysteresis (`COLD_BOOKING_RELEASE_BAND` = 0.5, keyed off the applied preset)
  holds the pierce until half a degree above target so it can't flap. An
  **unreadable** room does not bypass — under the summer regime the fail-safe is
  to stay locked (a manual Boost still pierces either way; err-off in summer, per
  the sensor-loss convention). The pierced booking falls through to the normal
  booking rungs, so it drives the reverse/destrat fans exactly like the row above
  (keyed off `applied[ZONE_A]`, no fan-logic change). Shared follows via
  `_desired_shared` (`_cold_booking_bypass` on either zone → eco). Audit reasons
  are tagged `lockout_booking` / `lockout_preheat` / `lockout_booking_eco` /
  `lockout_booking_quiet`. **First-winter watch:** confirm the room-below-target
  trigger fires only on genuinely cold booked sessions (not warm-fabric summer
  bookings) and that the 0.5 release band doesn't flap the pierce — the tagged
  reasons make both greppable in the export.
  **First field firing (2026-08-05 export, the day of the release).** The 08:00
  "1st pelsall squirrels" booking is the first live pierce, and it worked: lockout
  engaged (3-day avg 20.97 — high summer, so pre-feature this booking arrives
  frozen at ice), hall coasted to **18.5 coldest** overnight (~1 below the 19.5
  target), `_cold_booking_bypass` fired at 06:55 (`lockout_preheat`), the room
  climbed 18.5→19.5 and `booking_start.shortfall` was **0.0** (coldest 19.5 =
  target). Contrast the pre-feature 2026-07-31 08:45 booking, which arrived
  **+3.0 short** in similar lockout conditions — the deficit this closes. Shared
  followed to eco (`lockout_booking`) and correctly re-iced on an open kitchen
  contact. Occupants pressed Pause at 08:27 (room at 19.5, active young kids —
  the cutout, not a fault). **Three findings for the decision rules:** (1) *The
  trigger is eager by design.* This fired on a mild summer morning (outdoor 17.5)
  where the hall had merely drifted ~1 below target — not an "out-of-season cold
  snap." That is exactly the self-calibrating room<target behaviour chosen over a
  weather threshold, so working as specified; but note the hall will pierce
  readily whenever it drifts even slightly below 19.5 before a booking. A 1
  deficit at booking start is a real (if gentle) shortfall, so this pierce was
  reasonable — first data point for judging whether an engage-side deadband
  (pierce only when room < target − X) is ever wanted. **Did it need to heat?
  Arguably no — the trace says the room would have got there anyway.** The
  Rointes fired only *two brief* demand pulses (07:06, 07:38 — hard-confirmed
  real, not a modelled flag: the room rose 19.12 → 19.25 between them with nobody
  in and fans not yet on, so only the radiators could have done it); after 07:38
  the heaters were **off** (demand False) yet the floor still climbed 19.38 → 20.0
  on occupancy + the reverse fans, and the occupants Paused at 08:27. So the
  bypass delivered target with minimal heater input on a mild morning for active
  kids who then turned it off — the engagement is *correct per spec* but the
  *value* of a sub-~1 °C pierce is marginal. The eagerness comes partly from the
  trigger reading the **coldest** probe (18.5) while the room *averaged* 19.12
  (only 0.4 below target); an engage deadband, or judging the bypass on the
  *averaged* floor like the warm-up gate does, would spare firings like this while
  still covering the genuinely-cold case (e.g. 07-31's +3.0 short). Watch a few
  more before changing anything. (2) *One genuine flap,
  pre-existing, not the release band.* Stripping restart re-applies (the box was
  reloaded ~8× that morning deploying the release — each is a `None→…` re-apply),
  there was one real cycle: 06:55 ice→comfort, **07:10 comfort→ice**, 07:25
  ice→comfort. The 07:10 release was NOT the temp release band (room never hit
  20.0) — it was the **pre-heat window boundary oscillating**: room starting only
  ~1 below target, the recomputed lead crossed the shrinking gap, the window
  closed (`_cal_active` false → bypass off → lockout ice), then the room stopped
  rising, the lead grew, the window reopened. This flap is pre-existing (a
  near-target pre-heat window flaps comfort↔ice in an empty winter too); the
  lockout just makes it visible. Cost is a couple of extra Rointe commands, benign
  and self-resolved. **Fixed 2026-08-05: the pre-heat window now latches open** —
  once it opens for an event it holds until the event starts (or leaves the
  look-ahead), instead of recomputing `gap <= lead` every refresh and re-closing
  as the warming room shrinks the lead. Fixes the flap under lockout AND in winter
  (a near-target empty pre-heat flapped comfort↔ice there too), and is preferred
  over a bypass-side deadband because it addresses the root (the window boundary),
  not the symptom. See the pre-heat-window latch note below. (3) *No warm-up
  learned still (Q3).* Zero `warmup_sample` events ever — and not the flap's
  fault: learning gates on the *average* floor (19.12, only 0.4 below target,
  under the 0.5 start gate) while the pierce sizes off *coldest* (18.5), so the
  climb was too shallow by the averaged measure to start a sample. Q3 stays open
  until a booking begins meaningfully cold by the averaged floor.
- **The pre-heat window latches open (2026-08-05).** `_async_refresh_calendars`
  recomputes the lead every ~5 min, but once `cal_window[zone]` has opened for an
  event it is held open (`window = gap_min <= lead or self.cal_window[zone]`)
  until the event starts (`_is_on(cal)` takes over) or leaves the look-ahead (the
  `if not events` branch clears it). Without the latch, `lead` shrinks as the room
  warms and a bare `gap <= lead` test re-closes the window the moment the room
  nears target, flipping the zone out of comfort and back — observed in the first
  cold-booking pierce (comfort↔ice, 2026-08-05) but a general near-target pre-heat
  flaw (comfort↔eco/ice in an empty winter too). The `preheat_start` audit + hall
  pause-clear still fire only on the first open (the false→true edge). Trade-off:
  the room sits at comfort a little early if it reaches target before the booking
  — it cannot overheat past setpoint, and it removes the cold-arrival risk of the
  window closing mid-lead.
- **State-based summer setback (`summer_setback_mode` = off).** The seasonal
  lockout's default behaviour is to *block* — it freezes the hall to ice for the
  warm season and only a booking pierces it (cold-booking, above). This switch
  changes the lockout from a block into a **setback**: when it is on, an
  *occupied* hall that is genuinely cool is gently warmed to a low floor
  (`hall_summer_comfort_temp`, default 17.5) instead of frozen solid — heat then
  follows the building's **state**, not just the calendar. It answers audit
  finding F2 (in summer, occupancy alone — motion, override — could not previously
  get *any* heat; only a booking could). Mechanics, all in `_desired_zone`'s
  lockout rung (`_summer_setback_wants_heat`, hall/`ZONE_A` only): fires when the
  switch is on, the hall is occupied (`_cal_active` OR recent hall motion OR the
  occupied override) **and** the hall's *averaged* floor is below the setback
  floor. A warm hall (at/above the floor) or an empty hall still lands on ice, so
  the summer cooling fans keep the room in both those cases; an **unreadable**
  room does not heat (summer fail-safe: err off, like the cold-booking bypass and
  the summer fans). Delivered via the **eco** preset because the Rointe comfort
  setpoint floor is 19 °C and the setback wants ~17.5 — `_hall_eco_target` routes
  the eco push to the setback number whenever the preset reason is
  `summer_setback` (every other eco path keeps the ordinary eco number). The
  averaged floor (not the *coldest* probe the cold-booking pierce uses) is
  deliberate: a low-priority comfort floor should not over-fire on one cool end,
  and it sidesteps the cold-booking pierce's known eagerness (Q2 field note).
  Release hysteresis reuses `COLD_BOOKING_RELEASE_BAND` (0.5, keyed off the applied
  preset). Because the hall lands on a heating preset, the fans naturally run the
  **reverse/destrat** regime (keyed off `applied[ZONE_A]`, no fan-logic change) —
  the right direction for warming a cool room, not the cooling breeze. A
  genuinely cold *booking* still wins full **comfort** through the cold-booking
  pierce (checked first). **Default OFF** — a deliberate behaviour change the
  owner enables consciously; OFF is exactly the original lockout-as-block. Audit
  reason tagged `summer_setback`. **First-season watch:** confirm it fires only on
  genuinely cool occupied halls (not on a warm summer hall that merely dipped
  below 17.5 briefly), that the 0.5 band doesn't flap it, and — the real question —
  whether an occupied cool summer hall actually *wants* heat or whether the
  cooling fans alone suffice (the setback is comfort insurance for a cool
  shoulder-season day, not a heating-season tool).
- **"Will it get there on its own?" — the coast predictor (`coast_when_free` =
  off).** Audit finding F3: nothing suppressed heating the building would deliver
  for free (solar onto the big uninsulated roof, occupancy load, warm-fabric
  release) — the 2026-08-05 pierced booking spent two Rointe pulses then climbed
  19.38 → 20.0 with the *heaters off*. This switch adds the inverse of
  `preheat.required_lead_minutes`: during the **hall pre-heat window** (event not
  yet running), if the room is *measurably* warming fast enough to reach the
  comfort band by event start with a margin, it holds at **eco** (reason
  `preheat_coast`) instead of firing the radiators — the comfort guarantee kept,
  but delivered by the free gain. Pure logic in `coast.py`
  (`will_coast_to_target`), the most conservative module in the system because it
  is the inverse of a comfort guarantee (a wrong "it'll coast" = a cold arrival).
  **Comfort-lean by construction:** it declines heat only on an *observed* idle-
  room climb, never a guess — `rise_rate < MIN_RISE_RATE` (~0.5 °C/h), no reading,
  no rate, or too little spare time all fall through to heating; and it requires
  arrival with `TIME_MARGIN_FRAC` (0.30) of the lead still unused, so resuming
  heat is safe if the gain fades. **The rise rate is measured only while heaters
  are IDLE** (`_update_passive_rise` accumulates the coldest-hall reading only
  when `_heat_demand()` is false, clearing the buffer the instant demand appears)
  — so the signal is genuine free gain, not the radiators' own work, and the
  predictor *cannot oscillate* (applying heat wipes the evidence for withholding
  it). Re-evaluated every tick, hall-only. **Two scopes** (both under
  `coast_when_free`): (1) *pre-heat window* — deadline is event start, the room
  must be climbing fast enough to reach the band in time (`preheat_coast`);
  (2) *running, occupied booking* — deadline is now, so `_should_coast` is called
  with `gap_min=0` and the maths reduces to "already in the band AND still
  measurably rising", holding at eco (`booking_coast`) so free gain that is
  currently sustaining comfort isn't topped up by the radiators (the 2026-08-05
  case: an occupied booking whose floor climbed 19.4 → 20.0 with the heaters off).
  The `gap_min=0` reduction is the safety property — it can *never* withhold heat
  from an occupied room actually below comfort, only decline to top up one already
  comfortable and warming. An *unoccupied* running booking still drops to
  `booking_quiet`, not coast. The `coast_decision` audit event records the prediction
  inputs on the engaging edge (arrival checkable in a later export); diagnostics
  carry the live `passive_rise_c_per_min` + `coasting` flags. **Default OFF** —
  the owner enables it consciously and watches `preheat_coast` / `coast_decision`
  before trusting it. **First-season watch:** did coasted mornings actually
  *arrive* at comfort by event start (read `coast_decision` inputs vs the
  `booking_start.shortfall`), and did the idle-only rate ever mislead (a climb
  that stalled after the buffer measured it) — if shortfalls track coasted
  mornings, raise `TIME_MARGIN_FRAC`/`MIN_RISE_RATE` or revert to observe-only.
- **Office eco drift is unjudgeable** (the setpoint lives on the device and
  is never pushed); skipped rather than guessed.
- **Alarm suppression is away-aware (1.12.0).** `_alarm_armed` reads a real
  `alarm_control_panel` and only an *away*-type arm (`armed_away`/
  `armed_vacation` = empty building) drops a zone to ice; `armed_night`/
  `armed_home` (people sleeping/present inside — e.g. a sleepover) keep the heat
  on, and `triggered`/`arming`/`disarmed` never suppress. A booking still
  overrides the alarm entirely (`alarm_on and not cal_on`), and shared/water
  still need *both* panels away. Back-compat: a legacy `binary_sensor`/
  `input_boolean` mapping's `on` is treated as armed-away, so installs feeding
  the alarm through a helper keep pre-1.12 behaviour — but a binary cannot tell
  night from away, so night-awareness needs the real panel mapped. The config
  flow now accepts an `alarm_control_panel` for `alarm_main`/`alarm_office`; the
  owner maps which panel to which (main = hall + shared/kitchen/toilets/stores,
  office = office). The zone→conf-key pairing stays fixed (hall→`alarm_main`,
  office→`alarm_office`); only the entity is selectable.

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
hardware — office split/heat-pump pilot (a *modulating* device, so classic
weather compensation would apply to it — see the weather-comp note under Q17),
wall extractor fans, hall window contacts (they join the vent override
automatically when mapped). **Fit + map an office door/window contact** —
the office currently has *none*, so the `opening_ice` guard is blind there and
open-window ventilation drops get learned as fabric loss (the 2026-07-22
`zone_b_heatloss_pct` corruption to ~24 %/h; the `MAX_COOL_TICK_DROP` step guard
now catches the worst of it, but a mapped contact is the proper structural fix,
exactly as `zone_a_doors` protects the hall). **Reset `zone_b_heatloss_pct` to
~5 %/h** — the EWMA does not self-heal fast from that corruption.

## Architecture pointers

- `coordinator.py` — single 30 s reconciler; priority ladder in
  `_desired_zone` (disabled/hold → heating-paused → opening → boost → seasonal
  lockout *unless a cold booking pierces it* → alarm → booking/pre-heat →
  override/motion → empty).
- `preheat.py` — pure optimum-start maths (learned min/°C rates, Newton
  cooling with gap-normalised k). `fan_logic.py` — pure fan decision.
  `drive.py` — pure per-heater drive-to-target controller (staircase integral +
  heat-loss feedforward); `_reconcile_drive` in `coordinator.py` wires it in
  with the safety net.
- `audit.py` — event log + trace. `diagnostics.py` — the export.
- `docs/BEHAVIOUR.md` — original-automation → reconciler mapping and all
  behavioural fine print. Keep it and the README in sync with every change.
