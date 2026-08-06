# Control-logic audit: heat / cool / do-nothing, and the role of bookings

**Status:** design audit, no code changes. Written 2026-08-06 after the
drive-to-target work, prompted by the owner's observation that a *booking*
(a calendar event) — not the *state of the building* — is what currently
decides whether the hall heats or cools, and the request to add "don't heat
if the room will reach target on its own."

**Method.** Grounded in the actual code (`coordinator.py` `_desired_zone` /
`_desired_shared` / `_lockout_decision` / `_summer_active` / `_fan_target`,
`fan_logic.py`, `preheat.py`), in the field data gathered this month (the
`CLAUDE.md` open questions Q9/Q10/Q16/Q17 and the 2026-08-05/06 exports), and
in established building-control practice. Where something is not known, it is
flagged as an open question, not assumed.

---

## 1. What actually decides heat / cool / nothing today

Two decisions are made, and it helps to keep them apart:

**(a) Whether the hall heats — the priority ladder** (`_desired_zone`,
highest wins):

1. automation disabled / manual hold → hands off
2. hall heating paused → `ice`
3. opening held open → `ice`
4. boost → `comfort`
5. **seasonal lockout → `ice`** *unless a cold booking pierces it*
6. alarm (away) with no booking → `ice`
7. **booking / pre-heat window → `comfort`** (eco while unoccupied-but-running)
8. occupied override / recent motion → `eco`
9. empty → `eco` (someone elsewhere) / `ice` (building empty)

**(b) Fan direction** (`_summer_active` + `fan_decision`): the *regime* is
`summer` (forward, cooling) whenever the seasonal lockout is engaged, and
`winter` (reverse, destratification) otherwise — **but active hall heating
forces reverse regardless** (you never blow a cooling draught on people you
are heating). The regime only *runs* the fans on state (summer: occupied +
head-height air above `cooling_temp_high`; winter: ceiling-floor ΔT above
threshold + heat worth moving).

**The consequence the owner spotted.** In the warm season the lockout (rung 5)
sits *above* motion (rung 8), so **plain occupancy never reaches a heating
rung** — a cold, occupied, *un-booked* hall in summer stays at `ice`. Heat in
summer happens only via a **boost** (rung 4) or a **cold booking** (the rung-5
pierce, which needs a calendar event *and* the room below target). So a
booking is doing double duty: it is both the *occupancy signal* and the
*permission to heat at all*. Take the booking away and the room drops back to
`ice` + the summer fan regime — which is exactly what felt wrong.

---

## 2. Findings (grounded in the code + data)

**F1 — Direction is gated by the season, not the state.** The primary
heat-vs-cool determinant is the seasonal lockout: a **3-day average** outdoor
temperature vs a threshold (`_lockout_decision`). The live room temperature
only *modifies* this, via the cold-booking pierce. So a coarse rolling average
steers, and the thermometer only nudges. This is the root of the owner's
concern.

**F2 — Occupancy without a booking cannot heat in summer.** Verified in the
ladder: the `motion` rung is below the lockout rung, and the lockout returns
`ice`. Only a boost or a cold *booking* heats. Using a calendar event as the
enabler of heating (rather than just "people are here") is the specific
"booking logic" that doesn't generalise.

**F3 — There is no "will it get there on its own" term.** `required_lead_minutes`
predicts idle-gap *cooling* (Newton decay toward outdoor) and sizes the
pre-heat for the resulting deficit — but it never predicts *warming*. Nothing
in the system estimates solar gain (a big uninsulated roof), internal/occupancy
gain, or warm-fabric release, and nothing suppresses heating that the building
would deliver for free. Field evidence that this matters: on 2026-08-05 the
pierced booking fired only **two brief** Rointe pulses and then the floor
climbed 19.38 → 20.0 with the **heaters off**, on occupancy + the reverse fans
(recorded under Q10). The heat we spent was arguably unnecessary — the room was
going to get there.

**F4 — "Booking" conflates two independent things.** A calendar event today
means both *(i) the room will be occupied from time T* (→ schedule pre-heat,
count as occupancy) and *(ii) heating is now permitted despite the season*.
Good control separates these: **occupancy decides *when* to condition; the
room-vs-comfort state decides the *direction*.**

**F5 — The comfort target is a single fixed number (19.5).** It does not flex
with occupancy, activity, or season (Q19: operative vs air temperature; Q16:
the lockout threshold). The seasonal lockout is, in effect, a blunt stand-in
for "don't heat to 19.5 in summer" — a hard block instead of a lower target.

**F6 — The fan-regime *label* is tied to the calendar, not the need.**
`_summer_active` follows the lockout, so the default fan direction flips on a
3-day-average crossing. The *run* conditions are already state-based, and
active heating already overrides to reverse — but the season-labelled default
is the same coarseness as F1 (and drives the Q16 "does a lockout flip flap the
fan direction?" sub-question).

**What is already good and should be kept.** Optimum-start pre-heat with
learned warm-up + gap-normalised heat-loss rates (`preheat.py`); the
fail-safe directions (err warm in winter, err off for summer fans); the
drive-to-target loop (heaters actually reach setpoint); the seasonal
lockout's *intent* — do not run expensive electric heat in summer for
marginal comfort. The problem is the lockout's *implementation* as a hard
heating block, not its purpose.

---

## 3. Principles (what good control does here)

This is a **high-thermal-mass, leaky, electric-radiant, solar-exposed,
intermittently-occupied** building. The relevant established practice:

- **Deadband / state-based direction.** Heat below `target − deadband`, cool
  above `target + deadband`, do nothing in the band. Direction follows the
  thermometer, not the calendar. (The owner's intuition.)
- **Occupancy-based conditioning** (BS EN 15232 / demand-controlled): condition
  toward comfort *when occupied*, set back when not. Occupancy decides *whether*,
  not *which direction*.
- **Optimum start _and_ optimum stop.** We have optimum *start* (heat early
  enough to arrive at comfort). Its twin is optimum *stop* / **coasting** — a
  high-mass building keeps rising (or holds) after the heat is cut, so you stop
  early. The owner's "don't heat if it'll get there alone" is the pre-emptive
  version of the same idea: don't *start* if free gains will finish the job.
- **Free-heat rejection.** In a solar/internally-gained building, predict the
  gain and decline to heat when the room will reach comfort without it. Needs a
  gain estimate (solar from weather + time + the roof; occupancy load; fabric
  release).
- **Seasonal changeover as a setback, not a block.** Best practice changes over
  on outdoor temperature *and* actual demand, not a hard calendar/average gate.
  The lockout's cost intent is right; expressing it as a **lower summer comfort
  target / wider deadband / higher free-heat bar** keeps the saving *and* still
  heats a genuinely cold occupied hall.

---

## 4. Proposed architecture (state-first, lockout-intent preserved)

The reframe in one sentence: **occupancy decides whether to condition; the room
temperature vs a comfort band decides the direction; a predicted-gain term
suppresses heating the building will supply for free; the season shifts the
band rather than blocking heat.**

Concretely, replacing rungs 5–9 of the ladder with a state layer (rungs 1–4 —
disabled/hold, pause, opening, boost — stay as hard overrides on top):

1. **Occupancy state** (unchanged inputs): booked/running, recent motion, or
   empty. A booking is now *only* an occupancy schedule + pre-heat trigger — it
   no longer grants heating permission.
2. **Comfort band**, occupancy- and season-aware:
   - occupied → target `T_occ` (today's 19.5, but see Q19 activity-aware);
   - unoccupied-but-imminent (pre-heat) → `T_occ` by event start (optimum start);
   - empty → setback `T_set` / frost floor.
   - **Season shifts the band, it does not gate it:** in the warm season lower
     `T_occ` by a *summer setback* Δ and/or widen the heat deadband, so an 18 °C
     July hall with people in it is left alone, but a genuinely cold occupied
     hall still heats. This is the lockout's cost saving, kept, without F2.
3. **Direction from state:** room `<` band → heat; room `>` cool-threshold and
   occupied → cool (fans forward); in-band → passive (destrat fans only if a hot
   roof is worth harvesting).
4. **Predicted-gain suppression (the new piece, §5):** before committing heat,
   estimate whether the room reaches the band on its own within the window; if
   so, don't heat (or heat less). Fails toward comfort (see §6).
5. **Fan direction from what's happening, not the calendar:** reverse/destrat
   whenever heating *or* a hot stratified roof exists to reclaim; forward/cool
   when occupied and genuinely too warm; season only sets thresholds.

---

## 5. The "will it get there alone" mechanism

This is the piece the owner explicitly asked for, and the one that needs the
most care (it is the inverse of a comfort guarantee, so a wrong "it'll coast"
means a cold arrival). Grounded design:

- **What supplies free heat here:** (a) **solar** onto the uninsulated roof —
  large and time/weather-dependent (the destrat data shows the roof charging
  faster than the fans could clear it on a sunny morning, Q10); (b) **occupancy
  load** — a hall of active bodies (the 08-05 climb with heaters off was
  occupancy + fans); (c) **fabric release** — warm mass giving back heat.
- **Signals we already have:** the ceiling thermometer (an independent witness
  of the roof charging), outdoor + weather (cloud/condition), time of day,
  motion density as an occupancy-load proxy, and the learned warm-up/heat-loss
  rates.
- **The estimate:** a *passive* arrival predictor — will the room reach the
  band by the time it matters, from gains alone (no heater)? The mirror of
  `required_lead_minutes`: instead of "how long to heat up," ask "given the
  current rise rate (measured, not modelled) and predicted gains, will it
  arrive?" The **measured ceiling/floor rise rate** over the last N minutes is
  the most trustworthy input — if the room is already rising fast enough to
  arrive, don't add heat.
- **Learn it, don't guess it.** Add a *passive warm-up* observation (rise with
  `demand` false) alongside the existing heated warm-up learner, so the
  building teaches its own free-gain rate by season/time rather than a
  hard-coded solar model. (Ties to the project's "data before constants" rule.)

---

## 6. Trade-offs, risks, fail-safe directions

- **Comfort vs cost is the whole tension.** The owner wants *both* — no cold
  arrivals *and* no wasted heat. These conflict at the margin; a **deadband** +
  a **conservative** gain estimate is the resolution. Fail-safe: if unsure the
  room will coast, **heat** (a cold booked arrival is worse than a little waste)
  — but the deadband stops heating for a fraction of a degree.
- **Predictive = more to get wrong.** This is exactly why the drive
  self-validation (Q20) and conservative fail-safes must land first. Tonight's
  five-bug run is the cautionary tale: any predictive suppression ships behind a
  switch, defaulting to today's behaviour, and is validated against real exports
  before it is trusted.
- **Capacity is upstream of all of it (Q17).** If the hall physically cannot
  reach target on a cold night, no decision logic fixes it — that is kW. The
  first cold-night drive-to-target data will tell us whether that wall is real.
- **Don't lose the good parts.** Optimum start, the learned rates, the
  fail-safe directions, the drive loop — all carry over.

---

## 7. Decisions only the owner can make (the forks)

1. **Summer + cold + occupied + un-booked → heat, or not?** The owner leans
   *yes* (state-based). This is the F2 fix and the main fork. If yes, how much
   cooler is acceptable in summer before heating (the summer-setback Δ)?
2. **How aggressive is "will it get there alone"?** Bias toward not-heating
   (max saving, small cold-arrival risk) or toward heating (max comfort, some
   waste)? A deadband width + a conservative-vs-eager gain estimate.
3. **Comfort target: fixed 19.5, or occupancy/activity/season-flexed?** (Q19,
   Q16.)
4. **Decouple the fan regime label from the season entirely?** (state-driven
   direction; season only affects thresholds.)

---

## 8. Migration path (deliberate, not big-bang)

Given tonight, this is **not** a single rewrite of the ladder. Suggested order,
each step behind a switch defaulting to current behaviour, tested offline and
validated against field exports before the next:

1. **Land the drive self-validation (Q20)** — setpoint read-back + ceiling
   cross-check — so the controller can trust its own commands first.
2. **Add the passive-arrival learner and expose it** (observe-only): log a
   "would-have-coasted" prediction next to every actual heat decision, so we can
   *see* how often it would have been right before it changes anything.
3. **Turn the seasonal lockout from a block into a setback** (F1/F5): summer
   lowers the target / widens the deadband instead of returning `ice`. This
   alone fixes F2 (cold occupied hall heats) while keeping the saving.
4. **Wire predicted-gain suppression** into the heat decision, once the
   observe-only log shows it is reliable.
5. **Decouple the fan regime** from the season label (F6), last, as it is the
   lowest-value and most-coupled piece.

Each step is small, reversible, and evidence-gated — the opposite of how the
control ladder should be changed on a tired night.

---

## 9. Progress

- **Step 3 (F1/F5/F2) — SHIPPED behind `summer_setback_mode` (default off).**
  The seasonal lockout is now a *setback* rather than a hard block for the hall:
  an occupied hall below `hall_summer_comfort_temp` (17.5) heats to that floor
  via eco instead of icing (`_summer_setback_wants_heat`, `_hall_eco_target`,
  `reason` = `summer_setback`). This is the F2 fix (occupancy alone now earns
  heat in summer) and a partial F5 fix (a *lower summer target* rather than a
  block). Chosen over the deadband-widening framing because the setback floor is
  the more legible knob for the owner. Hall-only for now; office keeps the block.
  Owner enables it consciously — OFF is exactly the prior behaviour. First-season
  watch: whether an occupied cool summer hall actually wants heat or the cooling
  fans suffice.
- **Step 2 (passive-arrival / coasting, F3) — SHIPPED behind `coast_when_free`
  (default off).** During the hall pre-heat window, a *measured* idle-room climb
  that reaches the comfort band by event start with a time margin holds the zone
  at eco instead of firing the radiators (`coast.py::will_coast_to_target`,
  `_update_passive_rise`, reason `preheat_coast`). Owner chose *act immediately,
  comfort-lean*: it acts (not observe-only) but declines heat only on an
  observed climb, never a guess, and the rate is measured only while heaters are
  idle so it is genuine free gain and cannot oscillate. This is the direct F3
  fix. Now covers **both** the pre-heat window (deadline = event start,
  `preheat_coast`) and a **running, occupied booking** (deadline = now →
  `gap_min=0`, which reduces to "already in the band and still rising",
  `booking_coast` — the 08-05 heaters-off climb). The `gap_min=0` reduction is
  the safety property: it can never withhold heat from an occupied room below
  comfort. The `coast_decision` audit event + `passive_rise` in diagnostics are
  the validation instrument.
- **Step 1 (drive self-validation, Q20) — SHIPPED behind `drive_self_check`
  (default on).** Read-back (reported setpoint vs pushed, after a settle window)
  and an independent ceiling cross-check (heat requested, floor+ceiling flat →
  nothing responding). Notification-only. See CLAUDE.md Q20.
- **Step 4 (predicted-gain suppression inside a running session) — SHIPPED** as
  the running-booking scope of the coast predictor above (folded into step 2
  rather than built separately, since the same `will_coast_to_target` with
  `gap_min=0` expresses it).
- **Step 5 (decouple the fan-regime from the season, F6) — SHIPPED, and now the
  ONLY behaviour (no toggle).** `_fan_cooling_regime(warm, heating)` sets the
  cooling-vs-destratify direction purely from the hall's thermal state (cool only
  when the head-height air is genuinely warm AND the hall is not being heated;
  destratify otherwise; active heating always reverses). It briefly shipped
  behind a `fans_follow_state` switch / `cooling_changeover` select, but the owner
  wanted no admin toggle — so the season is gone from the fan path entirely and
  the direction is fully automatic. Reversals are kept rare by a 1 °C hysteresis
  band (`COOLING_DIRECTION_HYST`) rather than the season. This dissolves the Q16
  sub-concern (a lockout flip can't flap a direction that no longer reads the
  lockout).

**All six audit findings (F1–F6) are now addressed** — the season still sets
*thresholds* and the seasonal-lockout *intent* (don't run expensive heat in
summer for marginal comfort) is preserved, but direction, heating-permission and
the "will it get there alone" question are all state-driven, each behind a switch
defaulting to the prior behaviour. What remains is field validation against the
first cold, occupied, heated winter export (the instrument), not more building.
