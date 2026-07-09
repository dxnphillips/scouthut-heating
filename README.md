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

You can re-map any of these later via the integration's **Configure** button.

### Tunable controls (created automatically)

The integration creates its own helper entities — put them on a dashboard, no
restart needed to change them:

- **Numbers:** pre-heat lead time, no-motion eco timeout, door/window ice
  delays, seasonal-lockout threshold, hall comfort/eco/eco-low temperatures,
  water pre-heat lead time, water keep-on-after-motion.
- **Select:** boost duration (30/60/90 min).
- **Switches:** hall/office automation enabled, hall/office occupied override,
  water heater manual override.
- **Text:** ECO keyword blocklist (comma-separated).
- **Buttons:** boost hall, boost office, and cancel-boost for each.
- **Diagnostic sensors/binary sensors:** current & expected preset per zone,
  water state, seasonal lockout, opening-ice flags, manual-hold flags, boost
  flags.
- **Fan numbers:** ceiling-floor ΔT to start / stop, minimum run / off times,
  sensor-stale timeout, the summer warm-enough temperature, the heat-demand
  power threshold, and the winter recirculation floor cap.
- **Fan switches:** ceiling fans enabled, summer cooling mode, and "run when the
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

Three regimes, chosen by the **Summer cooling mode** switch:

- **Winter destratification** (default) → fans **reverse (up air)** when the
  ceiling-minus-floor ΔT is above the start threshold (default 3 °C) **and** the
  heat is worth moving — meaning either a radiator in *any* zone is drawing power
  (so office or shared heat leaking into the hall counts too), **or** the floor is
  still below the recirculation cap (default 24 °C). That second condition is the
  key one: like real destratification controllers, it runs on the ceiling-floor
  difference **decoupled from the heater's on/off cycle**, so it keeps harvesting
  *residual* heat after a heater has reached setpoint and cut out — pushing that
  already-paid-for warmth back down instead of letting it escape through the
  poorly-insulated roof. It runs for **loss reduction as well as comfort**, so it
  does **not** require the hall to be occupied. It stops when the ΔT falls to the
  stop threshold (default 1 °C), or once the heat is no longer worth moving (heater
  off *and* the floor has reached the cap). The two ΔT thresholds plus minimum
  run/off times (default 10 min each) prevent short-cycling. Defaults follow the
  documented practice for destrat fans (a few-degree ΔT band; a ~24 °C / 75 °F
  low-side limit).
- **Summer cooling** (only when *Summer cooling mode* is on) → fans **forward
  (down air)** when the hall is occupied and the floor temperature is above the
  warm-enough threshold (default 24 °C). The breeze is what cools people, so an
  empty hall never runs them.
- **Off / fail-safe** → the master opens whenever the fans are disabled or the
  Shelly fault is latched.

**When the ceiling / floor sensor is lost**, the behaviour is a tunable choice.
By default (*Fans run when sensor lost* on) it **assumes stratification and keeps
the winter fans running** while heat is being produced,
and raises a *sensor lost* notification. Turn that switch off to fail-safe to
fans-off instead. Either way the Shelly still owns motor safety, and a genuine
Shelly fault still forces the fans off.

**Fault handling.** If the Shelly publishes its fault as a boolean, map it and
the integration reads it directly. Until then it **infers** a fault from an
unexpected master-off (surviving the legitimate reversal dwell), refuses to
command the fans on, and notifies. Re-arming is deliberate — turn the Shelly
master back on, then toggle **Ceiling fans enabled** off→on; the integration
never auto-rearms in a loop. Before any reversal it also reminds whoever is
there to **set the transformer dial high** first (HA cannot check the dial).

---

## How it behaves

Instead of ~35 separate automations, a single **reconciler** re-evaluates every
zone on a 30-second tick (and immediately when a relevant sensor, calendar,
alarm or control changes). For each heated zone it picks the target preset by
this priority (highest wins):

1. **Automation disabled / manual hold** → the integration leaves the heater
   alone (manual hold is auto-set when it detects the Rointe app changed a
   heater during a booking).
2. **Door or window held open** past its delay → `ice` (the internal door only
   counts when an exterior opening is also open).
3. **Boost active** → `comfort` (bypasses the seasonal lockout).
4. **Seasonal lockout** (3-day *average* forecast temperature at/above the
   threshold; releases on a cold snap or low RealFeel) → `ice`.
5. **Alarm set with no booking** → `ice` and clears the occupied override.
6. **Booking or pre-heat window** → `comfort`, dropping to `eco` while
   unoccupied; events matching an ECO keyword stay on the lower `eco` setpoint.
7. **Occupied override or recent motion** → `eco`.
8. **Zone empty** → `eco` while someone is still elsewhere in the building,
   `ice` once the building is empty.

The **shared zone** follows either calendar / any motion / boost, and the
**water heater** turns on for its own pre-heat window, kitchen/toilet motion
(within the keep-alive) or the manual override, and off when the building is
alarmed. Two safeguards for the 15 L point-of-use tank override even the
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
