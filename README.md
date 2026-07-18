# Scout Hut Heating — Home Assistant integration

A custom [Home Assistant](https://www.home-assistant.io/) integration for the
**Pelsall Scout Hut**. It controls the Rointe electric heaters (hall, office and
the shared toilets/kitchen zone) and the kitchen water heater from booking
calendars, motion, door/window sensors, the intruder alarm and the weather
forecast — all configured from the UI, installable through **HACS**.

It is a from-scratch port of the two original Home Assistant *packages*
(`scout_hut_heating` and `scout_hut_water_heater`, kept for reference in
[`docs/reference/`](docs/reference)) into a proper config-flow integration, so
there are **no YAML packages to copy and no `<CHANGE_ME>` entity IDs to
hand-edit** — you map your entities with dropdowns instead.

> **Why an integration and not an "add-on"?** In Home Assistant an *add-on* is a
> Docker container managed by the Supervisor; add-ons cannot create helpers,
> scripts or automations inside HA. Logic like this is delivered as a **custom
> integration**, which is exactly what HACS installs.

---

## Installation (HACS)

1. In Home Assistant go to **HACS → ⋮ (top right) → Custom repositories**.
2. Add `https://github.com/dxnphillips/scouthut-heating` with category
   **Integration** and click **Add**.
3. Find **Scout Hut Heating** in HACS, click **Download**, then **restart Home
   Assistant**.
4. Go to **Settings → Devices & Services → Add Integration**, search for
   **Scout Hut Heating**, and complete the five setup steps.

Manual install (without HACS): copy `custom_components/scout_hut_heating/` into
your HA `config/custom_components/` folder and restart.

---

## Configuration

Setup is a five-step UI flow. Only the hall heaters, office heaters and the two
calendars are required; everything else is optional and any feature whose
entities you leave blank simply switches itself off.

| Step | You map… |
| --- | --- |
| **Heated zones** | Hall (Zone A) heaters, Office (Zone B) heaters, Shared-zone heaters, and the Rointe *comfort*/*eco* temperature `number` entities for the hall |
| **Motion sensors** | Hall, office, kitchen, gents and female-toilet motion/presence sensors (`binary_sensor` or `input_boolean`) |
| **Doors & windows** | Per-zone door and window contact groups, shared-zone windows, and the internal door |
| **Calendars, weather & alarm** | Hall & office booking calendars, weather entity, RealFeel sensor, the two alarm booleans, and the water-heater switch |
| **Ceiling fans & cooling** | Shelly fan master/direction switches and reverse button, the ceiling temperature sensor, the O1/O2 power sensors, and the Shelly fault boolean — all optional; leave blank to keep the fans off. The floor temperature and the Rointe *Effective Power* sensors are auto-detected from the heaters, so you normally leave them blank |

You can re-map any of these later via the integration's **Configure** button —
including *clearing* a mapping (leave the field empty and the entity is
unmapped). Safety latches and long-running clocks (manual holds, boosts, the
inferred fan fault, the water hygiene clock, the seasonal-lockout flag and
the fan anti-short-cycle timers) are persisted and survive a Home Assistant
restart.

### Tunable controls (created automatically)

The integration creates its own helper entities — put them on a dashboard, no
restart needed to change them. The easiest way is the **Create dashboards**
button (created with the boost buttons): one press builds a "Scout Hut"
sidebar dashboard with your *real* entity ids — including the mapped Rointe
and Shelly entities — and pressing it again regenerates it after new hardware
is mapped. The first tab is a deliberately simple **Home** view (status at a
glance plus the day-to-day buttons — boost, boost duration, pause/resume — and
no tuning sliders, so nothing gets nudged out of calibration by accident), plus
a **Temperatures (24 h)** graph that trends the head-height feels-like against
the ceiling/roof — the gap between the two lines *is* the stratification the
fans work on; set it as the app's default dashboard for a clean home screen.
The **Heating** and **Fans** tabs carry the operational controls and the tuning
sliders (Heating also splits the trends into a **Temperatures** and a separate
**Stratification** graph so the °C temperatures and the small ΔT/spread
differences each get their own scale); purely diagnostic rows are left off to
keep them uncluttered. Ready-made YAML equivalents are
in [`docs/heating_dashboard.yaml`](docs/heating_dashboard.yaml) and
[`docs/fan_dashboard.yaml`](docs/fan_dashboard.yaml) as the manual fallback.
Dashboard auto-creation touches a semi-internal Home Assistant API: on recent
HA versions the dashboard is written to storage and a notification asks for
one restart to surface it in the sidebar; if a future release reshapes the API
entirely, the button fails soft and points at the YAML files:

- **Numbers:** pre-heat lead time (the *maximum* — see optimum start below),
  hall/office learned warm-up rates, no-motion eco timeout, door/window ice
  delays, seasonal-lockout threshold, hall comfort/eco/eco-low temperatures,
  water pre-heat lead time, water keep-on-after-motion. The hall setpoint
  defaults (comfort 19.5 °C, eco 16 °C, eco-low 14 °C) are sized for **active**
  hall use per CIBSE Guide A (~17 °C for higher-activity spaces) and
  sports-hall practice (12–16 °C for games), anchored to the 18 °C classroom
  floor for the most sedentary regulars — crafts, closing circle, parents at
  pick-up. Use Boost for a genuinely sedentary cold evening and the ECO
  keyword tier for pure-activity lettings.
- **Select:** boost duration (30/60/90 min).
- **Switches:** hall/office automation enabled, hall/office occupied override,
  water heater manual override.
- **Text:** ECO keyword blocklist (comma-separated).
- **Buttons:** boost hall, boost office, cancel-boost for each, **pause /
  resume hall heating** (the occupant "too warm, stop" cutout — see below),
  **Create dashboards** (see above), and **Reset tunables to defaults** (restores
  every number/switch/select/text above to its built-in default; deliberately
  does not clear boosts, a hall pause, manual holds or a latched fan fault).
  Restored number values are clamped into the current slider bounds on startup,
  so an upgrade that tightens a range heals old out-of-range values automatically.
- **Diagnostic sensors/binary sensors:** current & expected preset per zone,
  water state, seasonal lockout, opening-ice flags, manual-hold flags, boost
  flags, the hall temperature spread (max − min across the hall heaters'
  readings — shows how patchy the room is side-to-side; expect it to collapse
  once the destratification fans mix the room), the ceiling-floor ΔT, and the
  head-height mix temp (0.75 × floor + 0.25 × ceiling — the air an occupant
  actually feels, and the single number the cooling start/stop and hot-breeze
  guard all act on).
- **Fan numbers:** ceiling-floor ΔT to start / stop, minimum run / off times,
  sensor-stale timeout, the summer warm-enough temperature, the heat-demand
  power threshold, and the winter recirculation floor cap.
- **Fan switches:** ceiling fans enabled, summer cooling mode (manual
  force-on), summer cooling follows season, and "run when the
  sensor is lost".
- **Fan diagnostics:** ceiling-floor ΔT, fan mode/direction, fan running, fault,
  heat-demand active, and sensor-lost flags.

---

## Ceiling fans (destratification & cooling)

Three reversible Vent-Axia fans run through one **Shelly Pro 2PM**. **The Shelly
script owns all timing and safety** — the 45-second coast-down dwell, the coil
verification, stall / low-tap protection and the latched fault. Home Assistant
only decides *when* the fans are wanted and *which direction*; it never
reproduces any of that timing. The reconciler evaluates the fans on the same
30-second tick as the heating.

Direction (Shelly O2 relay): **open = forward = down air = summer**;
**closed = reverse = up air = winter**. A live direction change always goes
through the Shelly **reverse button** (id 200); Home Assistant only writes the
direction relay directly while the master is off, and never while it is running.

Three regimes. The changeover is automatic by default: with **Summer cooling
follows season** on, the cooling regime is active while the seasonal heating
lockout is engaged and drops back to winter destratification when it releases
in autumn — nobody has to remember to flip anything, and direction reversals
stay seasonal-rare (best practice for these motors). The **Summer cooling
mode** switch remains as a manual force-on for out-of-season heatwaves:

- **Winter destratification** (default) → fans **reverse (up air)** when the
  ceiling-minus-floor ΔT is above the start threshold (default 2 °C, tuned
  for this under-radiatored hall; generic practice is ~3 °C) **and** the
  heat is worth moving — meaning either a radiator in *any* zone is drawing power
  (so office or shared heat leaking into the hall counts too), **or** the floor is
  still below the recirculation cap (default 24 °C). That second condition is the
  key one: like real destratification controllers, it runs on the ceiling-floor
  difference **decoupled from the heater's on/off cycle**, so it keeps harvesting
  *residual* heat after a heater has reached setpoint and cut out — pushing that
  already-paid-for warmth back down instead of letting it escape through the
  poorly-insulated roof. That residual-harvest path requires the hall to be
  **occupied** (switch **Winter fans need occupancy**, default on): an empty,
  unheated hut still stratifies from warm fabric, but the field cool-off samples
  measured a fan-mixed overnight decay ≈ the still one — so running on that
  ambient gradient with nobody there is ~150 W for no measurable retention or
  comfort. **Active heat demand always runs the fans regardless** (the savings
  case, including pre-heat). Turn the switch off to restore the legacy
  run-on-stratification-alone behaviour. It stops when the ΔT falls to the
  stop threshold (default 0.5 °C), or once the heat is no longer worth moving (heater
  off *and* the floor has reached the cap, or the hall is empty with no demand). The two ΔT thresholds plus minimum
  run/off times (default 10 min each) prevent short-cycling. Defaults follow the
  documented practice for destrat fans (a few-degree ΔT band; a ~24 °C / 75 °F
  low-side limit).
- **Summer cooling** → fans **forward (down air)** when someone is there to
  cool — recent hall motion, or a hall event actually running (so a seated
  group outside PIR coverage keeps its breeze) — and the floor temperature is
  above the warm-enough threshold (default 23 °C — a degree under the
  sedentary norm because hall users are active). **Exception — heating wins:**
  if the hall is actively being *heated* (a boost or booking has set comfort/eco,
  e.g. a boost forcing heat through the summer lockout), the fans run
  **reverse (destrat)** instead, so a cooling down-draught is never blown on the
  people the heat is for. That override follows the heating *preset*, not the
  radiator's on/off cycle, so the direction can't flap; the only cost is up to
  two reversals per summer boost. A **hot-breeze guard** holds
  the fans once the mixed air they would fold down to head height
  (0.75 × floor + 0.25 × ceiling) reaches the *max useful breeze temperature*
  (default 29 °C): past that a breeze gives rapidly diminishing benefit, so a
  notification asks for doors/windows to be opened. **Any** open mapped
  contact grants the fans an immediate provisional pass (a cross-draft makes
  them useful again) — kept while the venting at least *holds the line*: the
  test is trend-direction, not speed. The pass tracks the best (lowest) mix
  seen since venting began and is revoked only if the mix climbs ~0.5 °C
  above it — the measured signature of solar charge winning (~1.8 °C/h rise
  with nothing open), while genuine venting against a small indoor–outdoor
  gap may only manage a slow drift down and rightly keeps its fans. What
  matters is not which opening is open but whether it is actually making a
  difference. With nothing open, the fans resume by themselves once the air
  cools 1 °C below the threshold. The breeze is what cools
  people, so an empty hall never runs them; there is deliberately **no
  pre-cooling** — a fan's benefit is instantaneous wind-chill, so starting
  before anyone arrives would only add motor heat to an empty room. Above
  **35 °C** they are held off entirely with a
  notification, because air hotter than skin heats people instead of cooling
  them (per public-health guidance); ventilation and shade are the right tools
  at that point.
- **Off / fail-safe** → the master opens whenever the fans are disabled or the
  Shelly fault is latched.

**When the ceiling / floor sensor is lost**, the behaviour is a tunable choice.
By default (*Fans run when sensor lost* on) it **assumes stratification and keeps
the winter fans running** while heat is being produced,
and raises a *sensor lost* notification. Turn that switch off to fail-safe to
fans-off instead. Either way the Shelly still owns motor safety, and a genuine
Shelly fault still forces the fans off.

**Fault handling.** If the Shelly publishes its fault as a boolean, map it —
it is combined (OR) with the integration's own inference, since the script
cannot see every failure mode. Independently, the integration **infers** a
fault from an unexpected master-off (surviving the legitimate reversal dwell),
refuses to command the fans on, and notifies. A *power cut or wall-switch
cycle* is recognised and handled differently: the Shelly's entities go
unavailable, and when the device reboots with its outputs defaulting off the
controller simply re-establishes the wanted state on the next tick — fans
back within ~30–60 seconds, no latch, no re-arm. Crucially, while the master reads
unexpectedly off the integration **never re-commands it**: turning O1 on is
the Shelly script's re-arm gesture, so re-sending would defeat its own stall
latch and keep re-energising a faulted motor. Repeated reverse-button presses
that never move the direction relay (script missing/broken) also latch a
fault instead of retrying forever. Re-arming is deliberate — turn the Shelly
master back on, then toggle **Ceiling fans enabled** off→on; the integration
never auto-rearms in a loop, and the latch survives a Home Assistant restart. Before any reversal it also reminds whoever is
there to **set the transformer dial high** first (HA cannot check the dial).

---

## How it behaves

Instead of ~35 separate automations, a single **reconciler** re-evaluates every
zone on a 30-second tick (and immediately when a relevant sensor, calendar,
alarm or control changes). For each heated zone it picks the target preset by
this priority (highest wins):

1. **Automation disabled / manual hold** → the integration leaves the heater
   alone (manual hold is auto-set when it detects the Rointe app changed a
   heater during a booking — from the reported preset when the Rointe
   integration publishes one, otherwise from the reported setpoint, since
   each Rointe preset pins a known target temperature).
2. **Hall heating paused** (occupant "too warm, stop" cutout — hall only) →
   `ice`, above boost and bookings. Still frost-protected, and it holds the
   winter fans off. See *Pause hall heating* below.
3. **Door or window held open** past its delay → `ice` (the internal door only
   counts when an exterior opening is also open).
4. **Boost active** → `comfort` (bypasses the seasonal lockout).
5. **Seasonal lockout** (3-day *average* forecast temperature at/above the
   threshold; releases when the average falls half a degree below it, or on a
   **genuine cold snap** — RealFeel more than 2 °C under the threshold. Mild
   summer nights a degree under it neither release the lockout nor flap it)
   → `ice`.
6. **Alarm set with no booking** → `ice` and clears the occupied override.
7. **Booking or pre-heat window** (optimum start — see below) → `comfort`.
   An unoccupied room drops to `eco` only once the event has actually
   started — the pre-heat window always heats at comfort, since its whole
   purpose is reaching the comfort target by event start. Events matching an
   ECO keyword stay on the lower `eco` setpoint throughout.
8. **Occupied override or recent motion** → `eco`.
9. **Zone empty** → `eco` while someone is still elsewhere in the building,
   `ice` once the building is empty.

**Pause hall heating** (occupant cutout). The hall radiators are locked, so if
someone finds it too warm mid-session they have no way to turn them down. The
*Pause hall heating* button — ideally wired to a physical remote — forces the
**hall** to ice (frost protection still holds) above boost and bookings, and
holds the winter destratification fans off so they don't keep pulling roof-space
heat down onto them (the summer cooling breeze is left running, since that
*helps* a hot person). It's **hall only** — the office keeps its own heat — and
there is **no timer**: it clears only on a deliberate action (*Resume*, or a
hall **Boost** — the two are mutually exclusive, so boosting cancels a pause and
vice versa) or when a fresh booked session emerges from an idle gap (so the next
group arrives to a warm hall). Two *adjacent* bookings are handled for free: the
pause carried through a running session lifts at that booking's end, and the
next one inherits the still-warm room with its pre-heat naturally shortened.

**Physical button (blueprint).** To drive Boost or Pause from a physical remote
(e.g. a Shelly BLU RC Button), the repo ships an automation **blueprint**:
[`blueprints/automation/scout_hut_heating/hall_boost_pause_toggle.yaml`](blueprints/automation/scout_hut_heating/hall_boost_pause_toggle.yaml).
It turns one button press into a **toggle** — reading the matching status sensor
and pressing the opposite integration button — so a press flips Boost (or Pause)
on/off. Import it from *Settings → Automations & scenes → Blueprints → Import
blueprint* (paste the file's GitHub URL), then create one automation per button:
point each at that button's `event` entity and pick **Boost** or **Pause**. For a
two-button remote, import it twice — e.g. button 3 → Boost, button 4 → Pause;
boost and pause are mutually exclusive in the integration, so the two toggles
never end up both on. HACS installs the integration but **not** blueprints, so
this is a one-time manual import (the entity fields default to the standard ids;
change them only if you renamed the device).

**Optimum start.** The pre-heat lead is not a fixed number: each zone computes
it as *learned warm-up rate × how far the room is below **that booking's**
target* — a booking matching an ECO keyword pre-heats only to the eco-low
setpoint, not comfort. The deficit is measured from the **coldest** of the
zone's heater readings, not the average, so the warm end of a patchy room
cannot cut the lead short for the cold end — with a small extra margin when it is cold outside,
clamped between 15 minutes and the **Pre-heat lead time (max)** slider (the
safety cap — a room with no readable temperature also falls back to the cap,
so a cold start is never missed). When the event's start time is known, the
zone's **learned heat-loss constant** (the % of the indoor–outdoor gap lost
per hour, measured whenever the room coasts unheated) predicts how much
further it will cool before the pre-heat begins — Newton cooling toward the
outdoor temperature, never below the 7 °C anti-frost floor — so a booking
many hours away still gets a long-enough lead. Normalising by the gap is
what makes the learning season-proof: a July cool-off and a January one
teach the same fabric constant, only the gap differs, so the first cold
snap of autumn is predicted correctly instead of waiting for the model to
re-learn winter (measured here in July 2026: hall ~10 %/h, insulated office
~4.5 %/h; when the weather entity is unreadable the prediction assumes a
cold 5 °C outside and errs warm).

All the learned numbers are **fail-safe by construction**: the warm-up rates
are seeded at the slowest plausible value, so an unlearned zone uses
(effectively) the full cap — the old fixed behaviour — until real warm-ups
pull the rate down to the truth over a handful of bookings. Every real
comfort warm-up from cold is timed and folded in (exponentially smoothed and
clamped, so one door-open disaster can't poison it); a temperature *rise*
while unheated (July roof sun) is never mistaken for good insulation. The
hall keeps **two** warm-up rates — with and without the destratification fans
running — judged from the Shelly O1 power reading (a closed master with the
dial at zero doesn't count), because the fans materially change warm-up
speed. All the learned numbers are visible and adjustable — re-seed them
after any building change, or set the heat-loss constant to 0 to disable the
cooling prediction.

The **shared zone** follows either calendar / any motion / boost, and the
**water heater** turns on for its own pre-heat window, kitchen/toilet motion
(within the keep-alive) or the manual override, and off when the building is
alarmed. The switch is reconciled against its **real state**, not the last
command — a manual flip or a Shelly reboot is re-asserted on the next tick, so
frost protection cannot be defeated by one toggle, and the hygiene clock only
counts genuinely-powered time. Two safeguards for the 15 L point-of-use tank
override even the
alarm: it is powered whenever the shared zone nears freezing (≤3 °C, releasing
at 5 °C — the Speedflow's own frost stat only works while powered), and if the
tank has gone a week without a completed reheat it runs a 45-minute hygiene
heat-up so the stored water never sits lukewarm indefinitely between lets.
Only a continuous powered stretch long enough for a full reheat (45 min)
counts — brief dabs of use raise 15 L by only a few degrees and do not reset
the weekly clock. Hall comfort/eco setpoints are pushed onto the Rointe `number`
entities before a preset is applied, so slider changes take effect immediately.

A mapping from each original automation (A1–A35, W1–W9) to the reconciler is in
[`docs/BEHAVIOUR.md`](docs/BEHAVIOUR.md).

## Auditing the controller (diagnostics export)

The tuning constants (learned-rate seeds, clamps, thresholds) started as
textbook figures, not measurements of this building. To check them against
reality, the controller keeps a rolling audit log (bounded, persisted across
restarts) of everything it decides and learns:

- **`warmup_sample` / `cooloff_sample`** — every learning observation, accepted
  or rejected, with the raw inputs (duration, temperature change, the average
  indoor–outdoor gap, fan assistance and wattage, old and new value), so the
  EWMA behaviour can be re-derived. Cool-off samples carry the fan-running
  tally and average wattage too: the 2026-07-11 sealed test showed a fan-mixed
  hut sheds heat at roughly **half** the gap-normalised rate of a stratified
  one, and winter recirculation runs the fans through the evening cool-off
  routinely — the wattage separates the direction-dependent taps (summer
  forward ~195 W vs winter reverse ~158 W) a future fan-aware split would need.
- **`preheat_start`** — each time a pre-heat window opens: the lead chosen and
  every input it was computed from (rate, coldest reading, target, outdoor,
  heat-loss rate, gap to the event), plus `rate_key` (which learned rate drove
  the lead — the optimistic fan-assisted one or the base) and `fan_w_last` (the
  transformer tap the fans were last seen at, since the master is off while the
  pre-heat is idle and the live power reads zero).
- **`booking_start` / `booking_end`** — the ground truth: the coldest reading
  against the target at the moment each booking begins (a positive
  `shortfall` means the room arrived under target — lead too short; a
  consistently negative one means heating started earlier than needed), and
  the temperature and preset at the moment the controller saw the calendar
  event finish — so fan/preset changes shortly after can be read against it.
  The hall `booking_start` also carries `fan_w_last`, so a cold-arrival
  shortfall can be read against a fan speed the occupants had dialled down.
- **`motion`** — a PIR trip after its area has been quiet longer than the
  occupancy timeout (a genuine *arrival*, not every re-trigger while someone
  is already there — that would flood the log during a busy session). This
  is the only direct trace that the motion sensors are alive: motion
  otherwise shows up only indirectly, when it moves a preset or the fans.
- **`preset` / `fan_change` / `manual_hold` / `seasonal` / `water_hygiene` /
  `water_frost` / `fan_fault` / `overheat_holdoff` / `breeze_holdoff` /
  `condensation` / `fan_sensor_lost` / `heating_paused` / `heating_resumed`** —
  the actuation and safety record around those samples. Fan changes carry the
  decision inputs (`occupied`, `warm`, ΔT, demand, O1 watts), so a stopped
  fan is never ambiguous between "nobody there" and "not warm enough";
  preset changes carry the `reason` (which rung of the priority ladder
  decided them: `booking`, `preheat`, `booking_quiet`, `motion`,
  `seasonal_lockout`, `alarm`, `opening`, `boost`, `heating_paused`,
  `building_empty`, ...); and the hall pause records its temps/occupancy on
  activation and *what cleared it* (`manual`, `boost`, `preheat`, `booking_end`)
  — so how often people find the hall too warm, and in which sessions, is
  visible in the log.
- **A readings trace** — a week of 15-minute points of the exact computed
  values the decisions used (ceiling, the hall "floor" average and coldest
  reading, office, shared, outdoor, ceiling humidity, fan state, fan mode,
  hall occupancy, heat
  demand, and the O1 wattage). Fan mode and occupancy ride alongside fan state
  so empty-building winter fan-hours — the thing the occupancy gate suppresses —
  are measurable straight from the trace. The wattage matters because it encodes the manual transformer
  dial's tap — warm-up samples also carry their average O1 watts, so a moved
  dial perturbing the learned rates is visible in the data.

Download it from **Settings → Devices & Services → Scout Hut Heating → ⋮ →
Download diagnostics**. The JSON also contains every tunable's current value
against its default, the learned rates, a live snapshot of all readings, and
the **raw door/window/internal contact states** per group (with an
`any_open` roll-up) — distinct from the derived `opening_ice` latch, which
only trips after a contact is held open past the ice delay, so an open door
that hasn't been held that long reads open in `openings` while `opening_ice`
is still false. No credentials or tokens, so it is safe to share for analysis.

## Winter condensation watch

The Rointe anti-frost floor is fixed at 7 °C, but fabric-care guidance
(Historic England) prefers an 8–10 °C background for unoccupied buildings —
the risk being condensation and mould on cold fabric, not freezing. The gap
is covered by monitoring: the ceiling H&T's **humidity** sensor is
auto-discovered, and if the hall sits at ≥80 % RH below 12 °C for 12+ hours
during the heating season, a notification suggests a spell of background
heat (Boost) or airing the building on the next dry day. It clears itself
when the humidity drops or the hall warms; it never fires in summer.

## Field watch-list (open questions the data will settle)

The tuning constants started as researched values, not measurements of this
building; the audit trail exists to replace them with measured ones. Items
currently **awaiting field confirmation**, each with its pre-agreed
response (the full ledger with decision rules lives in
[`CLAUDE.md`](CLAUDE.md)):

- **Winter fan stop threshold (ΔT 0.5)** — if the first heated week shows
  the fans running continuously with ΔT plateaued just above it, the stop
  threshold moves to 1.0 (a slider).
- **Pre-heat cap (120 min)** — judged by `booking_start` shortfalls on
  cold-start mornings; slider headroom to 240 already shipped.
- **Warm-up & heat-loss learning** — seeds are fail-safe (warm); the first
  booked autumn weeks pull them to the building's truth. Success metric:
  arrival shortfall ≈ 0.
- **Calendar entity blips** — one mid-event dropout observed (2026-07-11);
  `booking_end`/`booking_start` pairs mid-slot now make it visible. If it
  recurs, an entity-off debounce gets built.
- **Hot-breeze guard (29 °C mix) and its vent trend-test (+0.5 °C
  revoke)** — calibrated from one measured solar-charge day; verified
  against the next heatwave. Also open: whether the hard 35 °C cutoff
  should drop toward 32 for a children's building.
- **Fan dial stability** — O1 wattage travels with every learning sample;
  if warm-up rates cluster by dial tap, band-aware learning gets
  considered.
- **Destratification savings forecast** (500–800 kWh/winter net) — checked
  against degree-day-normalised Rointe statistics after the season.
- **Fan-mixed cool-off samples** — the sealed test showed mixing roughly
  halves the gap-normalised loss rate, and winter recirculation will run the
  fans through many evening cool-offs. Every `cooloff_sample` now records its
  fan tally; if the winter samples cluster into clearly different fans-on /
  fans-off rates, the constant gets split — until then one EWMA absorbs both.

**Settled by field data** (the method works — measured, not guessed):

- **Sealed-hut fan-clearing test** (run 2026-07-11, hot still evening,
  everything shut): fans dropped the *ceiling* reading fast, but
  gap-normalised the whole hut lost heat at barely **half** its natural rate
  (~14 %/h mixed vs ~26 %/h stratified). Stratification parks the hottest
  air against the roof — the building's best exit — and mixing pulls it away
  while adding ~200 W of motor heat. Verdict: unoccupied evening
  fan-clearing is counterproductive; summer fans stay occupied-only, exactly
  as coded.

---

## Please test before relying on it

This is a behavioural re-implementation of the original packages, not a
line-for-line copy, and it could not be executed against a live Home Assistant
in the environment it was built in. The Python imports, the config flow and the
full decision table are covered by an offline test harness, but **verify it on
your own system** — watch the diagnostic sensors and the heater presets through
a booking, an opening event and a boost before trusting it unattended.

## Development / tests

The reconciler is covered by a self-contained test suite (90+ scenarios across
all three zones, the cross-zone links, openings, seasonal lockout, boost and the
auto-mapping) that runs without a full Home Assistant install — it stubs the HA
API surface the integration uses:

```bash
pip install pytest
pytest
```

## License

MIT — see [LICENSE](LICENSE).
