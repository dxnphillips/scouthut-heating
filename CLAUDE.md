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
- **Toggle surface was consolidated (2026-08-06) to a common-sense minimum.**
  The three fan-direction switches (`summer_mode` / `summer_follows_season` /
  `fans_follow_state`) were removed entirely — **fan direction is now fully
  automatic from live room state** (`_fan_cooling_regime`; a brief
  `cooling_changeover` select was tried and dropped — the owner wanted no admin
  toggle at all). Four near-universal "on"
  switches were **baked into permanent behaviour** (no toggle): `cold_booking_heats`
  (a cold booked room always heats), `drive_self_check` (the Q20 notification-only
  command checks always run), `winter_fans_need_occupancy` (empty-hut recirc is
  never run — the field data settled it), and `summer_setback_mode` — the latter
  not "baked on" but **subsumed**: heating was decoupled from the season entirely
  (2026-08-07, see the unified-heating bullet below), so an occupied cool hall now
  heats toward comfort on its own with no toggle and no separate setback floor.
  Kept as real switches: the six
  operational ones (per-zone automation-enabled + occupied-override, water
  override, fans-enabled), `fans_run_on_sensor_loss` and `drive_to_target` (both
  genuine fail-direction / big-behaviour escape hatches), and the one
  field-unvalidated feature `coast_when_free` (default off — the deliberate
  "validate before trust" opt-in; it is the only feature that can *withhold*
  heat, so it stays opt-in on principle).
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
- **The cool-off learning is self-protecting against unsensored openings
  (v1.25.4).** Two layers on top of the gap/tick guards. (1) *Robust EWMA*
  (`MAX_COOL_STEP_FRAC` = 0.25): no single cool-off may move a zone's
  `heatloss_pct` more than 25 % of its current value, so one anomalous reading
  can only nudge the baseline, never yank it (the office was repeatedly leaping
  5→16 %/h off one open-window sample). Bounds BOTH directions, including the
  dangerous low one (a spuriously-tight reading shortening the lead → cold
  arrival). (2) *Out-of-family reject + alert* (`COOL_OUTLIER_RATIO` = 3.0):
  once the EWMA is a real learned baseline, a sample implying a loss rate >3× it
  is physically impossible for the fabric — it is an open window/door with no
  contact (the office has none) or a probe glitch, so the whole sample is
  rejected (`updated_cooling_k` returns k unchanged) and `_opening_inferred[zone]`
  latches. `_update_opening_inferred_alarm` pushes a *"window or door open?"*
  notification to every companion-app device (`_push_companion` → every
  `notify.mobile_app_*`) plus a persistent notification, once per episode,
  cleared on the next in-family cool-off. Self-gating: at the coarse 25 seed a 3×
  multiple (0.75) exceeds `MAX_COOL_K` (0.5), so it cannot fire until a
  trustworthy low baseline exists — exactly when it is safe. The ratio is
  generous (a genuine winter ~2× doubling still folds in), and the leaky hall
  (which can lose fast and has real contacts) rarely trips it. `cooloff_sample`
  now carries `outlier`; a new `opening_inferred` event records the edge. This
  does NOT retire the office contact sensor — inference is after-the-fact and
  can't tell a window from a probe unfreeze — it is the backstop for surfaces
  that will never be wired.
- **The warm-up learning is self-protecting too, in the OPPOSITE (dangerous)
  direction (v1.25.5).** Cool-off corruption inflates loss = err warm = wasteful;
  warm-up corruption makes the room look like it heats too FAST (low min/°C),
  which *shortens* the pre-heat lead and risks a **cold arrival** — the exact
  failure pre-heat exists to prevent, and most likely now because these rates are
  first being learned in summer when solar gain on the big roof (plus occupancy,
  plus fan-delivered ceiling heat) can make a warm-up read far faster than the
  radiators alone. Two layers in `updated_rate`, mirroring cool-off: (1) *Robust
  EWMA* (`MAX_RATE_STEP_FRAC` = 0.25) caps how far one sample moves the rate either
  way — no seed problem, it only slows the legit learn-down. (2) *Out-of-family
  FAST reject* (`RATE_OUTLIER_RATIO` = 3.0): a sample implying heating >3× faster
  than the learned rate is free gain, not the radiators, so it is rejected whole.
  Gated to an **established** rate (`WARMUP_ESTABLISHED_FRAC` = 0.9 — pulled >10 %
  below the `MAX_RATE` 60 seed): at the seed the real rates (25–46) sit close
  enough that an early reject would block legitimate learning-down, so during that
  phase only the robust cap protects it. `warmup_sample` carries `outlier`;
  **audit-only, no push** (the fan-attribution split already routes fan-assisted
  climbs to the fan rate, and "the sun helped" is nothing to act on). The
  model-derived values (booking-hold margin, drive feedforward) inherit this via
  their learned inputs; the coast passive-rise predictor stays as-is (idle-only,
  margin-guarded, transient, default-off).
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
  the master is off. **A device reboot is exempt from the master-off fault**
  (v1.25.2): a wall-switch flick or power blip takes the master `unavailable`
  then `off` (outputs default off), which is not a manual kill — `_fan_fault`
  resets the expectation and re-commands from scratch instead of latching. A
  reconcile poll landing on the `unavailable` catches it, but a reboot briefer
  than the 30 s poll would slip through as a straight available→off and latch a
  false `master_off` (field 2026-08-08: master `unavailable` 00:32:08 → `off`
  00:32:09, ~1 s, latched a phantom fault while the Shelly itself reported none —
  the tell the owner spotted). `_note_fan_master_state`, called off the
  master's state-change event (which fires even when no reconcile coincides with
  the blip), records the transient unavailability so the next tick recognises the
  reboot. A genuine stall-latch (the Shelly script opening the relay) leaves the
  device online — no `unavailable` — so it still latches correctly.

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
   **Frost-to-comfort is NOT a pre-heat job — the cap can only buy ~3.5 °C
   (2026-08-27, worked from the first clean cold-fabric rate, Q3).** The lead is
   linear in the deficit (`lead = rate × (target − predicted)`, `predicted` floored
   at the 7 °C anti-frost temp the heating holds even when "off"). From a hall that
   has fallen to **frost 7 °C**, the rise to 19.5 is **12.5 °C**, so at the measured
   ~44 min/°C the model wants **~550 min (~9 h)**; at the current learned 34.4,
   **~430 min (~7 h)**; winter's `OUTDOOR_MARGIN` adds ~10 % → **~8–10 h**. But the
   lead **clamps to `preheat_minutes` (155 now)**, and 155 min ÷ 44 = **~3.5 °C** —
   a frozen hall would climb 7 → ~10.5 and arrive **~9 °C short**. Even the 240 cap
   buys only ~5.5 °C. **So the lever is never a bigger cap — it is not letting the
   hall reach frost before a booking:** an overnight eco *pre-charge* (Q17's
   fabric-pre-charging) so the morning starts from ~16 °C and a real 3–4 °C climb
   the pre-heat window *can* deliver (this morning: 18.75 → comfort in 44 min).
   Caveat on the 9 h figure: it extrapolates a *near-target* 1 °C rate (measured
   where loss is highest) 4–5× past any observed climb — the early 7→12 climb is
   faster per °C (smaller gap) but fights cold-mass soak (Q17), so treat 7–10 h as
   an order of magnitude, and note Q17's open question of whether frost→19.5 in one
   session is even reachable before the capacity/soak wall.
3. **Warm-up rates (seeded 60 min/°C, fail-safe).** Expect `warmup_sample`
   events to pull the hall (fans-assisted and base) and office rates toward
   truth over the first booked weeks; `booking_start.shortfall` ≈ 0 is the
   success metric. Target 19.5 °C is now reachable (old 22 never was), so
   completed warm-ups finally exist to learn from.
   **First clean cold-fabric sealed warm-up (2026-08-27, owner-confirmed all
   external doors + windows shut).** The hall (occupancy-driven, no booking —
   people arrived to a cold hut) climbed floor **18.75 → 19.75 °C in 44.1 min,
   fans-assisted (reverse, 219/222 ticks, ~153 W), reached target cleanly** —
   an observed **44.1 min/°C**, the *slowest accepted* fans sample yet, and
   notably slower than even the base (no-fan) rate (40.2). That inversion is not
   the fans hurting: the fans rate had been biased *fast* by the one solar-assisted
   midday sample (2026-08-22, 15.9 min/°C, an 11:16 climb on a warm roof), and this
   pre-sunrise cold-fabric climb is the honest cold-start pulling it back —
   `zone_a_warmup_rate_fans` EWMA'd **30.2 → 34.4**. Base (40.2) and fans (34.4)
   are converging on the true ~40+ from opposite sides. **The destrat mechanism was
   visibly working** (Q10): across the climb the ceiling *fell* while the floor
   *rose* — gap ceiling−floor collapsed **1.85 → ~1.0** — so this was delivery, not
   heat pooling uselessly at the apex. **Trustworthy baseline** (the point of the
   sealed condition): overnight `cooloff_sample`s were textbook (~1 °C/2 h, gap ~4,
   ~12.2 %/h, no outlier, no `opening_inferred`) — nothing leaked. **Caveat: still
   mild** — outdoor was 15.5–16.7 °C; a real winter cold-start (colder out, colder
   fabric) will read slower still, so expect the fans rate to keep drifting up past
   34–40 as winter mornings land (the fail-safe, arrive-warm direction). This was
   occupancy, not a booking, so there is *still* no `booking_start.shortfall` to read
   — the next cold *booked* morning remains the pre-heat validation (Q2/Q14).
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
   **Backstopped 2026-08-11 (v1.25.4): the robust EWMA + out-of-family reject**
   (`MAX_COOL_STEP_FRAC`, `COOL_OUTLIER_RATIO`, see the cool-off self-protection
   bullet above) now stops one open-window sample corrupting the constant AND
   pushes a "window/door open?" alert to every companion-app device — so the
   office no longer silently re-inflates between manual resets while the contact
   is unfitted. The contact (a) is still the proper fix; this is the safety net.
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
    **RESOLVED (v1.26.3, field 2026-08-21).** The leak fired: a sal-vation eco
    booking drove the *shared* zone to comfort (its own over-heating bug, also
    fixed — see the shared eco-keyword note), and its demand ran the *hall* fans
    reverse while the hall sat warm on ice — "nothing to reclaim". Verdict: pure
    cost (the hall was already warm; pre-heat/heating warms the rooms on their own,
    fans deliver nothing cross-zone). Fix: the hall destrat `demand` is now
    hall-specific — `hall_demand = demand and heating` (heating = hall in
    comfort/eco), so a neighbour's demand never spins the hall fans; the hall
    fans run on the hall's own heating, and the `recirc` path already covers
    "the hall wants more heat delivered". `_heat_demand` stays building-wide for
    diagnostics. If a future co-heating test proves real office→hall spillover
    (Q12), revisit — but the room is warm-enough-on-ice case makes delivery moot.
16. **Seasonal lockout threshold (`seasonal_lockout_temp` = 15). MOSTLY MOOT
    (2026-08-07): the season no longer gates heating** (unified `_room_wants_heat`
    gate — a cold booked/occupied room heats whatever the season, a warm one does
    not). The threshold now only decides when the *condensation watch* pauses
    (warm season) versus runs (cold season), so a slightly-wrong value can at worst
    mis-time the condensation clock by a fortnight — the "booked session arrives
    cold under an engaged lockout" concern below is gone (it heats on room state
    now). The RealFeel cold-snap clause and its flapping are likewise heating-dead;
    they only nudge the condensation-watch boundary. Retained below as the original
    analysis. A textbook
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
    **RESOLVED (F6, 2026-08-06):** fan direction no longer follows the season at
    all — it is fully automatic from the hall's thermal state
    (`_fan_cooling_regime`), so a lockout flip cannot flap the direction and this
    sub-question is moot. Boundary flapping is instead prevented by the
    `COOLING_DIRECTION_HYST` (1 °C) hysteresis on the warm/cool line.
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
    read. **First partial read on (a)–(c) (2026-08-27, occupied cold-fabric climb,
    sealed — see Q3).** This was a *small* climb near target (18.75 → 19.75), not a
    frost cold-start, so it does not settle the capacity wall — but against the three
    discriminators it points AWAY from stratification-as-the-limit and toward
    *delivery working*: (a) the ceiling−floor gap was **small and shrinking** under
    load (1.85 → ~1.0), not a big stuck 5–6 °C — heat was not pooling; (b) the floor
    kept **rising** to target and got there (soak/time behaviour, consistent with the
    44 min/°C being a *rate* limit, not a ceiling); (c) the ceiling **fell** while the
    floor rose (the reverse fans pulling the apex down — the opposite of "made heat
    pooling at the roof"). So *at 18.75, mild outdoor, fabric already warm*, the hall
    is delivery-fine and rate-limited, not stratification-capped. The capacity/soak
    question is still open for the case that matters — a *deep cold-fabric* winter
    climb (frost → comfort, Q2), where cold mass soak and a big loss gradient could
    still expose a wall this gentle climb never approached. **A sharper discriminator
    already exists in HA, also code-free** (the
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
    a code change to the control law. **The occupant-side lever now exists
    (2026-08-07): a Boost drives to `comfort + boost_offset`** (see the boost
    bullet below), so "felt cold at 19.5 → pressed boost" both *relieves* the
    complaint in the moment and *records* it (`boost` events); a recurring boost
    pattern at a satisfied 19.5 is the data that would justify raising the standing
    setpoint. The measurement gap is the deeper issue:
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
    **Re-entry false-positive fixed (v1.22.1, 2026-08-07 field export).** The
    first v1.22.0 export caught `drive_setpoint_rejected` firing on the *shared*
    heaters (kitchen/gents) 8 s after the shared zone entered comfort — impossible
    for a genuine 10-min-settled judgment. Cause: the withdrawal path writes the
    comfort *number* to the plain target while the heater is on ice (live setpoint
    still 7), stamping the settle clock; on re-entry to comfort `_drive_push`
    early-returned on the unchanged number, so the reassert was skipped and the
    read-back judged against the stale stamp while the heater still read 7. The
    shared zone exposed it because it now flips in/out of comfort often (the hall
    mostly holds comfort through a booking). Fix: a `_drive_driven` set tracks the
    driven episode; on the withdrawn→comfort edge `_drive_push(..., force=True)`
    re-asserts the preset AND restarts the settle window even when the number is
    unchanged, so the read-back only judges a heater that has been *continuously*
    driven for the full window. General (not shared-only), fail-safe (abstains
    longer, never flags sooner).
    **Startup false-positive fixed (v1.24.3, 2026-08-08 field export + owner
    confirmation).** A booked-evening export caught the read-back flagging *all
    four* hall heaters ~10 min after a restart (the phone showed "wrapping up
    startup"), reporting the old 19.5 setpoint while the drive pushed the hold's
    20.0–20.5 — then **they matched once the cloud caught up, with nothing pushed**.
    So the Rointe cloud is much slower to reflect a setpoint *just after a restart*
    than the settle window assumes (it is NOT a real setpoint-not-landing, and NOT
    a comfort ceiling — the pushes did land, late). Fix: `_within_startup_grace`
    holds BOTH drive self-checks (read-back and no-response) off until the
    integration has been up `DRIVE_STARTUP_GRACE_MINUTES` (25). Same fail-safe
    direction (abstain longer). This is why the earlier "the hall can't be driven
    above comfort" alarm was a false read — the above-comfort push *does* land, it
    was just the post-boot cloud lag; the v1.24.2 comfort-number diagnostic is kept
    to confirm should a genuine non-adoption ever recur mid-run (not at startup).
    **Settle window widened 10 → 30 min (v1.24.4, 2026-08-08 comfort-number
    export).** The diagnostic proved the mechanism: the `comfort_number` entities
    all held the pushed 20.0–21.0 (max 30, no clamp) and one heater was actively
    *heating* to 21.0 — but the climate's **live setpoint** (what the read-back
    reads) lagged the number by **10–27 min** through the cloud (a heater flagged at
    10 min was matched + heating by the 27-min export). So the device adopts, just
    slowly; the 10-min settle window was simply too short for this cloud. 30 min
    clears the observed lag; a genuine never-adopt stays mismatched past it and is
    still caught. This — not the startup grace — is what stops the routine flags on
    the hold's ice↔comfort re-engagements (each re-push restarts the settle clock).
    **Action-gated (v1.25.1, 2026-08-08 morning export).** With 1.25.0 deployed and
    the hall driving hard on a booked morning, the read-back still flagged heaters
    that were *actively heating* — live setpoint one 0.5 step behind the pushed
    value while the drive staircased *upward* (the cloud is perpetually a quantum
    behind a moving target). But a heater reporting `hvac_action == heating` has
    demonstrably accepted the command — its setpoint IS landing, just late. The
    phantom-push fault this check is for looks different: a heater sitting **idle**
    at a stale-low setpoint. So the read-back now only flags a mismatch when the
    heater is **not heating** (idle) — using the `action` already in diagnostics.
    This is the more fundamental fix: live-setpoint-vs-pushed was the laggiest
    possible signal; `action` reflects the real response. A stuck heater still gets
    caught (it goes idle at its stale setpoint → flagged).
    **Satisfied-target guard (v1.25.3, 2026-08-09 daytime export).** The action gate
    left one false-positive class uncaught: three hall heaters flagged while **idle
    at the 20.0 target the room had already reached** (`demand` false, coldest 20.0),
    their live setpoint merely lagging through the cloud. The action gate can't
    rescue a *satisfied* heater because a satisfied heater is idle, not heating — yet
    reaching the target could only happen if the pushed setpoint was **adopted**. So
    the read-back now clears on a second independent proof-of-adoption: the heater's
    own probe having reached the pushed target (`probe >= pushed − tol`). It flags
    only a heater that is **short of target AND idle AND not reporting our setpoint**
    — the genuine phantom-push signature (idle at a stale-low setpoint *while the room
    is still cold*). Between them, the two proofs (probe reached target, or actively
    heating) cover every way a landed command looks, leaving only real non-adoption.
    The residual single-heater active-climb flag (one heater short+idle mid-climb as
    the cloud lag brushes the 30-min window) stays a *widen the settle window* call
    per the decision rule, not a tolerance change.
21. **Fan-direction thrash from a spurious comfort flip (field 2026-08-29, v1.28.1
    export — candidate debounce, NOT yet built; confirm root on 1.29.2).** On a warm
    occupied afternoon the ceiling fans reversed **3× in 84 min** (11:49 forward →
    12:41 reverse → 13:03 forward), each a ~5-min coast/spin sequence. Root: fan
    *direction* is keyed to the hall heat state (`_fan_cooling_regime`: heating →
    reverse so it never wind-chills the warmed people; warm + not-heating → forward
    breeze), and the hall preset **flapped comfort↔ice**. The `COOLING_DIRECTION_HYST`
    (1 °C) guards the warm/cool line but **nothing debounces the heating→direction
    coupling**. The flap traced to **one spurious `ice→comfort` at 12:17 on a
    uniformly ~22 °C hall** (trace coldest 21.5, no probe near the 19.5 gate; reason
    `motion`). Leading cause — unconfirmed on 1.28.1 — is a **transient Rointe hall-probe
    drop-out hitting the err-warm heat path**: outdoor was 14.5 °C (< comfort), so the
    v1.26.2 ceiling+outdoor reinforcement *cannot* withhold and a drop-out past the
    2-min grace errs warm → comfort → reverse fans; the natural warm→ice at 13:03 then
    forced it back to forward. All four hall heaters read healthy/warm/idle at export,
    and the building `demand` keeping things active was the **shared toilets** (600 W
    each), not the hall. **Candidate fix (two layers):** (a) *fan-direction debounce*
    — a wanted reversal must persist N min before actuating, so a transient preset blip
    causes zero physical reversals (mitigation: only fully cures episodes shorter than
    N; the 12:17 comfort lasted ~46 min so a 15–20 min debounce cuts 3 reversals to ~1,
    not 0); (b) the real cure is **upstream** — stop the spurious comfort by widening
    the err-warm grace / holding last-known-warm longer when every *other* reading says
    warm. **Decision rule:** on 1.29.2, read `hall_fire`/per-heater `heating_status` at
    the next such flap — if a hall probe is unreadable at the `ice→comfort` edge while
    the others read warm, it is the err-warm transient (fix (b)); if a probe genuinely
    dipped, different. Build the debounce (a) as the cheap symptom guard regardless
    (fire/fault must bypass it; it only *delays* a confirmed-real reversal, never blocks).
    Pairs with Q16 (the F6 note resolved *season*-driven direction flapping but left the
    *heat-state*→direction coupling undebounced — this is the first field case of it).
22. **External evidence review (2026-08-29, standards/literature audit) — triage
    against the current code.** A thorough external report graded the system near
    BS EN 15232 Class A and **independently reproduced the frost-deficit arithmetic**
    (7 °C floor → ~12.5 °C rise → ~8 h needed vs a 155-min cap delivering ~3.9 °C → a
    cold booking arrives ~8 °C short — same as Q2). Its genuinely useful, actionable
    items and the ones to reject (it evaluates a ~2–3 week-stale snapshot and misses
    the fire-hold, drive self-checks, boost-above-comfort, hold-margin, sleepover/
    night-arm heating, transient-blip guard, per-heater capture, eco-keyword/hall-
    specific fan demand):
    - **ACTIONABLE — winter frost fix (needs owner sign-off; the real Q2 lever).**
      (a) *Winter fabric setback ~9 °C* for the empty hall in the cold season instead of
      the 7 °C ice floor — shrinks the morning deficit and cuts timber thermal swings /
      condensation (Historic England, CIBSE church guidance). Cost: standing energy to
      hold 9 vs 7 in a leaky hall is real; and it re-adds a *season branch on the empty
      floor only* (not on occupied heating — narrower than the decoupling it doesn't
      touch). (b) *Overnight pre-charge* — on a forecast-cold night before a morning
      booking, hold eco ~16 overnight (ideally in the Economy-7 window) so the morning
      starts warm and the 155-min window's achievable 3–4 °C lift is enough (this
      morning: 18.75 → comfort in 44 min proves the small lift works). **This is the
      actual fix**; the cap is a minor lever (even 240 buys only ~5.5 °C). Cost genuinely
      uncertain (a leaky hall stores little; E7 only wins if the charge abuts the booking)
      — **run as a measured winter trial, do not switch on faith.**
    - **ACTIONABLE — cheap.** Seasonal-flag *hysteresis* (separate engage ≥15 / release
      ≤13.5–14 on the 3-day mean, drop/deadband the RealFeel edge) to stop the ~19-flaps/
      week — low value (the flag only times the condensation watch now) but a few lines.
    - **ACTIONABLE — cloud-write chatter (optional, bigger).** *Asymmetric* minimum
      preset dwell — hold before a *demotion* to a cooler preset, but promote to a warmer
      preset immediately (never delay heating a cold room). Protects the unofficial Rointe
      Firebase API (no documented rate limit). Same root as item 21's flap; the fan
      debounce is the targeted fix, this is the separate cloud-load fix.
    - **OWNER-SIDE SAFETY (highest consequence, not code).** Dedicated *pipe/tank frost
      protection* (trace heating / frost stat — the 7 °C air floor does not protect pipe
      runs in cold voids), and *verify the kitchen water heater meets HSG274 Part 2*
      (50–60 °C thermal cycle for Legionella on the ≤15 L POU unit). Do before deep winter.
    - **REJECT — fights the design or already handled.** *Do NOT* make the season gate
      office/shared heating (reverses the deliberate `_room_wants_heat` decoupling; a warm
      room already doesn't heat whatever the outdoor). The "shared runs to 21 on warm days"
      waste is largely gone (eco-keyword v1.26.3 + the room-below-target gate). "k drift
      8.7→12.5 is contamination" is unproven — winter wind/infiltration can make 12.5 real,
      and the robust EWMA + outlier + tick-drop guards already reject impossible samples
      (the office just converged *cleanly* 7.34→3.94, disproving "gates too weak"). Coast/
      optimum-stop already exists (deliberately off for winter). RLS-vs-EMA and an RC/
      non-linear preheat rewrite are low return for this plant.
    - **RE-EVALUATE ON 1.29.2 DATA.** The frost fix's value, the drive-saturation question
      (`hall_maint` vs floor deficit = capacity wall vs stratification), the destrat kWh
      saving (`hall_kwh` fans-on/off), and item 21's flap root all read directly from the
      first cold heated export once upgraded.

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
  summer fans stop (fail-safe for people). **`_heat_demand` ignores phantom power
  on frost-protected heaters (v1.26.1).** Demand reads the Rointe *effective
  power*, which is **modelled** (100/50 % of nominal) when a device reports no
  real value (Q17), so an idle heater on the 7 °C ice preset can briefly read
  >20 W — not real heat. If no zone is on a heating preset (comfort/eco), demand
  is False whatever the sensors say. Without this, a phantom reading on an ice
  heater asserted demand and, coinciding with a brief ceiling/floor `dt None`,
  took the *winter* sensor-loss path (keep running → **reverse**) instead of the
  intended *summer* one (stop) — flipping the cooling fans to a full reversal on a
  hot afternoon (field 2026-08-11: 4 spurious reversals, every heater idle on ice
  in a 24 °C hall). The fix restores the documented summer-stop behaviour.
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
  above the recirc target) just leaves the fans off rather than blowing anything.
- **Destrat recirculation chases the applied setpoint, not a fixed cap
  (2026-08-07).** The winter recirc "is the heat worth moving?" test gates on the
  floor being below `_hall_desired_setpoint()` — the temperature the hall is
  actually being driven to (the applied preset's comfort/eco value, or the 7 °C
  anti-frost floor when the hall is on ice) — capped by `fan_recirc_max_floor_temp`
  as an absolute ceiling. Previously it used the fixed `fan_recirc_max_floor_temp`
  (24) alone, which is a *winter comfort ceiling* blind to what the occupants want:
  a field export (2026-08-07) caught the fans destratifying stored ceiling heat
  down onto an **eco-low cleaning slot** (target 14) whose room already sat at 16
  — the booking was on ice (room above its low target, lockout engaged, no
  pierce), so floor 16 < 24 wrongly read as "worth harvesting". Now the fans chase
  the same goal the heaters do: an ice preset returns 7, so a warm-enough / frozen
  hall harvests nothing (`recirc_ok = floor < min(desired_setpoint, cap)`). Because
  a booked zone only lands on ice once its room is at/above the booking target, the
  applied setpoint is a faithful proxy for "what the occupants want". The
  *demand* path (`worth_moving = demand or recirc`) is unchanged and remains the
  building-wide Q15 sub-question (office heat still runs the hall fans via demand).
- **Fan direction is fully automatic from room state — no toggle, no season
  (F6, finalised 2026-08-06).** The whole `summer_mode` / `summer_follows_season`
  / `fans_follow_state` switch tangle, and the `cooling_changeover` select that
  briefly replaced it, are **gone**. `_fan_cooling_regime(warm, heating)` decides
  the direction from live state alone: active heating → reverse (never wind-chill
  the people being warmed — the `heating` gate); a genuinely warm (`warm`,
  head-height above `cooling_temp_high`), not-being-heated hall → forward (cool
  the hot people); a cool or unknown hall → reverse/off (a warm reading is
  *required*, so unknown warmth never blows a draught on assumption). The season
  does not enter into it — a warm hall gets a breeze whatever the calendar says,
  a cool one destratifies. (Heating itself is now also season-independent — see
  the unified `_room_wants_heat` gate; the season only pauses the condensation
  watch. This is the F1/F6 decoupling carried through to the heat side too.)
  **Reversals stay rare
  via hysteresis, not the season:** `warm` is computed with a `COOLING_DIRECTION_HYST`
  (1.0 °C) band keyed off the previous `fan_mode` — once cooling has started the
  room must drop a full degree below `cooling_temp_high` before the direction
  flips back, so a hall hovering at the threshold can't flap the heavy fans
  forward↔reverse. `_summer_active` was removed: the hall-pause breeze exception
  is now `allow_destrat` (a pause suppresses the reverse regime, leaves the
  forward breeze — so a warm paused hall gets its breeze in *any* season), the
  overheat/breeze notifications gate on `_fan_cooling_wanted`, and warm-up-rate
  attribution keys off `fans_enabled` (heating forces reverse, so the fans assist
  any heated warm-up regardless of season). **First-shoulder-season watch:**
  confirm direction changes only on real warm↔cool transitions
  (`fan_change.direction`) and that the 1 °C hysteresis is enough to keep
  reversals rare (widen `COOLING_DIRECTION_HYST` if not); and that a warm hall
  getting a forward breeze is always wanted (if not, raise `cooling_temp_high`).
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
  **The 15-min trace now carries `hall_fire` and `drive_off` (2026-08-28) so a
  climb is retrospectively attributable** — `hall_fire` is the count of hall
  heaters reporting `hvac_action == heating`, `drive_off` the largest overdrive
  (°C above target) the drive had wound on. Together they answer the question the
  2026-08-27 export could NOT: was a warm-up the radiators or free gain (a climb
  with `hall_fire` 0 is fans+occupancy+solar, as the 08-05 heaters-off climb was),
  and how hard was the drive pushing through it.
  **The saturation + consumption gap is now closed too (2026-08-29).** Field
  screenshots confirmed each Rointe heater exposes sibling sensors —
  `sensor.<heater>_heating_status` (idle/heating/**maintaining**),
  `sensor.<heater>_energy` (kWh, `TOTAL_INCREASING`),
  `sensor.<heater>_surface_temperature`, `sensor.<heater>_effective_power` —
  auto-discovered from the heater's device (`_heater_sensor_map`, mirroring the
  power discovery). The trace now also carries **`hall_maint`** (count of hall
  heaters throttled to *maintaining*) and **`hall_kwh`** (sum of hall energy, for
  a within-trace consumption delta); the diagnostics per-heater block (hall,
  office AND shared) now includes `heating_status` / `energy` / `surface` /
  `effective`. This is the **definitive Q17 discriminator**: heaters short of
  target AND pinned at full `heating` (hall_maint 0) = capacity wall (drive can't
  help); heaters short but at `maintaining` = heat reached the local probe, not
  the far field = stratification/soak (fans/lead, not more kW). `hall_kwh` deltas
  give the Q10 duty/saving signal without cross-referencing HA statistics.
  `hall_fire` stays as the always-available `hvac_action` fallback for installs
  whose status sensors don't map. **First-winter watch:** on the first cold
  heated climb read `hall_maint` vs the floor deficit to settle capacity-vs-
  stratification, and `hall_kwh` fans-on vs fans-off for the destrat saving.
- **Boost drives ABOVE comfort, not just to it (2026-08-07, owner insight).** A
  boost used to return the comfort preset and nothing more — so pressing it while
  the room was already at the comfort setpoint was a *no-op* (the drive was
  already targeting comfort), exactly when the occupant is saying "still too
  cold." Now `_drive_comfort_target` returns `comfort + boost_offset` (a slider,
  default +2 °C, clamped to the 30 °C Rointe max) while `_boosting(zone)` is true,
  so the drive chases the higher target for the boost duration and reverts on
  expiry. `_hall_desired_setpoint` follows it, so the destrat fans keep delivering
  the boosted heat to head height. Shared follows either room's boost. This is the
  occupant-side lever for **Q19** (felt-cold at a satisfied 19.5 in this
  cold-radiant hall is real): a boost is the "I want more than the standing
  setpoint right now" signal, and the slider lets the owner tune how aggressive it
  is. It only ever aims *warmer*, time-boxed, so it cannot leave the hut overheated.
- **A booking "holds" the hall above comfort so it can't dip during the slot
  (2026-08-07, owner insight; `preheat.hold_margin`).** The drive is slow (0.5 °C
  staircase, 15 min/step) and only fires *below* target, so on a cooling evening it
  catches a booked hall reactively at comfort and then lags — the room undershoots
  while the radiators spin up. Because a booking means comfort is wanted for the
  whole slot, `_booking_hold_margin` raises the drive's target (and the gate's
  engage point) a little above comfort to pre-empt the fall. The margin is sized
  from **both** learned rates — Newton cool-off (`zone_a_heatloss_pct` × the
  comfort−outdoor gap = °C/h lost) × a response lead scaled by the learned warm-up
  rate (sluggish hall → longer lead) — so it is exactly the head-start the fall
  warrants: bigger on a cold night, ~zero on a mild one, and zero when the rates
  are unlearned or the `booking_hold_cap` slider (default 1.5, 0 = off) is 0.
  **Model-based** (comfort + outdoor + the two learned constants, NOT the live
  indoor trend), so applying the heat can't collapse the margin and oscillate — a
  stable function of conditions. Hall only, running comfort bookings only (an eco
  booking targets eco-low; the pre-heat still owns *arrival*, this owns holding the
  floor *through* the slot); suppressed while the coast predictor is holding at eco
  (opposite decisions). It only ever aims *warmer*, capped, so it cannot overheat.
  **The trade is explicit:** a booked hall runs a fraction warm on cold evenings (up
  to the cap) to guarantee it never dips *below* comfort — which is the point.
  **The Rointes only accept 0.5 °C setpoints** (field-confirmed 2026-08-07), so the
  continuous margin is used raw as a *comparison* threshold at the gate but
  `_drive_comfort_target` rounds the final driven target to the 0.5 grid — an
  off-grid value is silently rounded by the device and would wobble the read-back
  self-check. (All other setpoints — the comfort/eco sliders, the boost offset, the
  drive staircase — were already on the grid; the continuous hold was the only leak.)
  Diagnostics carry `drive.hold_margin`. **First-winter watch:** read the trace at a
  cooling-evening booking — did the floor stay at/above comfort through the slot
  (hold working) or still dip (raise the cap / `HOLD_LEAD_BASE_MIN`)? And confirm it
  self-zeros on mild evenings. Pairs with Q17 (a capacity-limited hall may not
  *reach* the held target — the `drive_capped` alarm surfaces that).
- **Heating is decoupled from the season — one gate, occupancy == booking
  (2026-08-07).** The seasonal lockout no longer blocks heat. Both a booking and
  bare occupancy heat on the **same** self-calibrating test, `_room_wants_heat(zone,
  target)`: the room's **coldest** heater probe (freshness-gated via
  `_rointe_stale_min` — a frozen Rointe reading is dropped) is genuinely below the
  target it is asking for (`_booking_target` — comfort, or eco-low for an
  ECO-keyword booking; comfort for occupancy). Release hysteresis
  (`COLD_BOOKING_RELEASE_BAND` = 0.5, keyed off the applied preset) holds the
  decision half a degree past target so it can't flap. **A warm room lands on
  ice**, freeing the summer cooling fans; **an unreadable room errs WARM** (heats —
  the heating fail-safe direction, and benign because the Rointe governs the real
  firing against its own probe, so a genuinely warm room won't fire anyway; the old
  cold-booking bypass erred *off*, but only because the summer lockout it pierced
  made err-off the season's fail-safe). The whole season apparatus on the heating
  side — the `seasonal_lockout` block, the `_cold_booking_bypass` pierce
  special-case, `summer_setback_mode` + `hall_summer_comfort_temp` — collapsed into
  this one rule; the season flag survives only to pause the condensation watch.
  **Occupancy and booking are no longer different behaviours** (owner insight): a
  cold occupied hall heats to the *same* comfort target a booking does. Booking
  uniquely adds **foreknowledge** (a pre-heat lead, warm from minute one) and
  **persistence** (it holds the target through the slot; an unoccupied *running*
  booking still drops to eco via `booking_quiet`). **`booking_quiet` counts
  positive presence the PIR can't see (2026-08-08, sleepover field incident):**
  its "occupied" test is recent hall motion OR the manual occupied-override switch
  OR a **Night/Home alarm arm** (`_alarm_present` — people sleeping inside), so a
  sleepover full of *still* sleepers is not demoted to eco and left to cool (the
  05:33 export caught the hall at 17.5 °C on a cold night because the sleeping
  scouts tripped no PIR → `booking_quiet` → eco). A genuinely empty booking (no
  motion, no override, not night-armed) still drops to eco. Bare occupancy heats
  only while presence is confirmed (recent motion, the occupied override, **or a
  Night/Home alarm arm** — v1.26.0) and stops when it lapses. **The night-arm
  presence now heats WITHOUT a booking too** (v1.26.0, field 2026-08-11): a
  sleepover with nothing on the calendar used to frost-protect a room full of
  sleepers, because `_alarm_present` was only consulted inside the booking branch
  (`booking_quiet`) — bare occupancy checked only motion/override. A Night/Home
  arm is the same positive-presence signal the PIR can't see when everyone is
  still, so it now drives the bare-occupancy heat trigger as well: a night-armed
  cold zone heats to comfort (reason `sleepover`), a warm one still lands on ice
  (`occupied_warm` — the `_room_wants_heat` gate is unchanged), and an *away* arm
  is still an empty building (iced upstream, never a sleepover). Per-zone via
  `ZONE_ALARM`: the hall/shared read the main panel, the office its own — so a
  night arm heats only the zones whose panel is armed. Shared is deliberately
  unchanged (it still needs a booking or shared-area motion — a sleepover warms
  the toilets when someone actually gets up, not all night). Reason strings:
  `booking` / `preheat` / `booking_warm` / `booking_eco` /
  `booking_quiet` / `preheat_coast` / `booking_coast` for bookings; `motion` /
  `occupied_override` / `sleepover` (heating) and `occupied_warm` (warm → ice) for
  occupancy; the `lockout_*` tags are gone. **Shared (kitchen/toilets/stores) heats toward
  comfort too now** (2026-08-07): `_desired_shared` warms the block to
  `shared_comfort_temp` (via `_shared_wants_heat`, the shared analog of the gate —
  coldest shared probe below target, err-warm on unreadable) whenever it is
  genuinely in use — a hall/office booking is running OR there is motion in the
  shared PIRs (`WATER_MOTION_AREAS` = kitchen/gents/female). A warm shared block
  rests at eco (`shared_warm`); motion only in the hall/office (nobody in the
  shared rooms) keeps the lighter eco floor (`motion`), so a cleaner in the hall
  doesn't drive the toilets to comfort. The drive-to-target loop already owns the
  shared comfort setpoint, so no new wiring. Reasons: `booking` / `shared_motion`
  (→ comfort), `booking_eco` / `shared_warm` / `motion` (→ eco), `building_empty`
  (→ ice). No seasonal gate. **The shared follows the eco-keyword too (v1.26.3).**
  Originally *any* running booking drove the shared to comfort, so a sal-vation
  eco-keyword session heated the toilets to 19.5 while the hall correctly sat at
  eco-low — heat the shared did not need (it was already above eco), whose demand
  then spun the hall fans with nothing to reclaim (field 2026-08-21). Now the
  shared follows the **warmest active booking**: `eco_booking = a booking is active
  AND no active hall/office booking is non-eco` → the block rests at the eco floor
  (`booking_eco`), the Rointe idling once warm enough so no demand is created. A
  concurrent **non-eco** office/hall booking still wins (→ comfort), so an eco
  booking can never downgrade the shared below what a real session needs. **First-shoulder-season watch:** confirm bare motion heating
  to comfort is wanted (it is eager — any drift below 19.5 while occupied heats;
  present-only bounds the cost) and that a warm occupied hall correctly gets the
  cooling breeze rather than heat; if motion proves too eager, an engage-side
  deadband (heat only when room < target − X) or judging on the *averaged* floor is
  the first lever (see the eagerness note in the field record below).
  **Field record of the predecessor (2026-08-05, the cold-booking pierce, since
  generalised). The forensics still apply — the unified gate reads the same coldest
  probe with the same release band.** The 08:00
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
- **The pre-heat window latches open, keyed to the event (2026-08-05, refined
  2026-08-06).** `_async_refresh_calendars` recomputes the lead every ~5 min, but
  once the window has opened for an event it is held open (`window = gap_min <=
  lead or latched`, `latched = _preheat_open_for[zone] == start`) until the event
  starts (`_is_on(cal)` takes over) or leaves the look-ahead. Without the latch,
  `lead` shrinks as the room warms and a bare `gap <= lead` test re-closes the
  window the moment the room nears target, flipping the zone out of comfort and
  back — observed in the first cold-booking pierce (comfort↔ice, 2026-08-05) but a
  general near-target pre-heat flaw. **The latch is keyed to the specific event's
  start** (`_preheat_open_for`), not a bare `cal_window` bool: a running event and
  an empty look-ahead both clear the key, so the latch holds *this* pre-heat open
  but does NOT bridge into the *next* booking — without the keying, two bookings
  inside the 120-min look-ahead held comfort continuously across the empty gap
  between them (audit found 2026-08-06). A fresh first event is judged on `gap <=
  lead` again, so the hall rests at eco between back-to-back bookings and pre-heats
  the second one on its own optimum-start lead. The `preheat_start` audit + hall
  pause-clear still fire only on the first open. Trade-off: the room sits at
  comfort a little early if it reaches target before its booking — it cannot
  overheat past setpoint, and it removes the cold-arrival risk of the window
  closing mid-lead. (`_preheat_open_for` is not persisted; after a restart mid-
  pre-heat the next refresh re-derives it, at most one benign comfort↔eco cycle at
  the near-target boundary.)
- **Warm-enough reads reject a frozen Rointe value (2026-08-06).** The Rointe
  cloud can freeze while the entity still reads `available`, so every path that
  decides "is the room warm enough?" — pre-heat sizing (`_zone_preheat_minutes`),
  the unified heat gate (`_room_wants_heat`), the coast predictor and the drive
  no-response witness — passes `stale_min` (`_rointe_stale_min`, the `fan_sensor_stale_minutes`
  window) into `_zone_room_temp` / `_zone_climate_temps`, which now drop a heater
  whose `last_reported` is older than that. A frozen-high reading previously
  under-led a cold start into a cold arrival; it now reads as None → fail-warm
  (the pre-heat falls back to the cap). The fan ΔT reference and the diagnostic
  spread deliberately omit `stale_min` (a frozen value is harmless there).
- **A transient room-reading drop-out doesn't flip a warm room to heat
  (v1.26.2).** `_room_wants_heat`'s err-warm fail-safe is right for a *sustained*
  loss but wrong on a *blip*: a ~17 s Rointe hall-probe drop-out on a hot
  afternoon (field 2026-08-11) made a genuinely warm hall read None → err warm →
  flip ice→comfort→ice, and the comfort preset reversed the cooling fans (one
  spurious reversal per blip). Two layers before err-warm: **(A)** the zone's own
  last good reading is cached (`_last_room_temp`) and held for `ROOM_READING_GRACE_MIN`
  (2) — the most reliable "other reading", only seconds stale, safe in every
  season, so a blip changes nothing; **(C)** past that grace, on a *sustained*
  loss, the independent ceiling (hall only) can withhold heat — but only when the
  **outdoor is also at/above target**, which rules out the winter-stratification
  trap (a warm ceiling capping a cold floor with residual roof heat → a cold
  arrival). Both conditions must say warm, else it still errs warm. This is the
  root fix for the fan-reversal flapping; v1.26.1 (phantom demand on ice) was a
  real but different path that did not cover the transient-comfort cascade.
- **State-based summer setback — SUBSUMED and removed (2026-08-07).** This was a
  default-off switch (`summer_setback_mode`) that softened the seasonal lockout
  from a block into a setback: an occupied cool hall warmed to a low floor
  (`hall_summer_comfort_temp`, ~17.5) via eco instead of icing, answering audit
  finding F2 (occupancy alone earned no summer heat). When heating was decoupled
  from the season entirely (unified `_room_wants_heat` gate, above), this became
  the *default* behaviour and better — an occupied cool hall now heats toward full
  **comfort**, not a 17.5 floor, with no toggle and no separate setback number. The
  switch, `hall_summer_comfort_temp`, `_summer_setback_wants_heat`, the
  `summer_setback` reason and the `_hall_eco_target` setback branch are all gone
  (`hall_summer_comfort_temp` is now an orphaned entity to delete owner-side, like
  the two learned-rate entities). The one open question it raised — does an
  occupied cool summer hall actually *want* heat, or do the cooling fans suffice? —
  now resolves per room state: a genuinely cool hall (coldest probe below comfort)
  heats; a warm one gets the forward breeze.
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
- **Fire fallback holds EVERYTHING off, manual-clear (v1.27.0).** The fans' 230 V
  supply is hardware-cut on the panel's fire output (the real safety — a cut line
  HA can never override; wire the *Shelly's own supply* so the device power-cycles
  cleanly). But HA does not otherwise know a fire happened, so on the next power
  blip it would re-arm the fans. `_handle_fire_event` listens on the HA bus for
  the `scouthut-alarmnotification` (Texecom Alerts) integration's
  `texecom_alerts_event` with an `event_type` in `FIRE_EVENT_TYPES` (`Fire`,
  `KeypadFire`) and latches `_fire_hold`: `_desired_zone`/`_desired_shared` return
  **ice** (frost-protect), `_desired_water` returns **off**, and `_fan_target`
  returns **off** — and because the *wanted* fan state is off, a Shelly reboot
  mid-fire re-establishes off, never the fans. The latch beats everything
  (automation-disabled, manual hold), is **persisted** (survives a restart mid-
  fire), audits `fire`/`fire_cleared`, and pushes a persistent + companion-app
  alert. There is **no auto-clear** (no clean "fire over" signal, and the hut
  should stay off until checked): only the **Clear fire hold** button
  (`async_clear_fire_hold`) releases it. Rising-edge only (repeat fire events are
  a no-op). No-op if the alarm integration is not installed (the event never
  fires). Diagnostics carry `state.fire_hold`.

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
**Safety, before deep winter (2026-08-29 evidence review, Q22):** fit *dedicated
pipe/tank frost protection* (trace heating or a frost stat) on vulnerable pipe
runs — the 7 °C Rointe air floor protects room air, not pipes in cold voids; and
*verify the kitchen water heater meets HSG274 Part 2* (a genuine 50–60 °C thermal
cycle for Legionella on the ≤15 L point-of-use unit) and that the hygiene clock
guarantees it.

## Architecture pointers

- `coordinator.py` — single 30 s reconciler; priority ladder in
  `_desired_zone` (disabled/hold → heating-paused → opening → boost → alarm →
  booking/pre-heat → override/motion/night-arm → empty). Booking and occupancy both heat
  only when `_room_wants_heat(zone, target)` (room below target); a warm room
  lands on ice. No seasonal rung — the season no longer gates heat.
- `preheat.py` — pure optimum-start maths (learned min/°C rates, Newton
  cooling with gap-normalised k). `fan_logic.py` — pure fan decision.
  `drive.py` — pure per-heater drive-to-target controller (staircase integral +
  heat-loss feedforward); `_reconcile_drive` in `coordinator.py` wires it in
  with the safety net.
- `audit.py` — event log + trace. `diagnostics.py` — the export.
- `docs/BEHAVIOUR.md` — original-automation → reconciler mapping and all
  behavioural fine print. Keep it and the README in sync with every change.
