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
   **Scout Hut Heating**, and complete the four setup steps.

Manual install (without HACS): copy `custom_components/scout_hut_heating/` into
your HA `config/custom_components/` folder and restart.

---

## Configuration

Setup is a four-step UI flow. Only the hall heaters, office heaters and the two
calendars are required; everything else is optional and any feature whose
entities you leave blank simply switches itself off.

| Step | You map… |
| --- | --- |
| **Heated zones** | Hall (Zone A) heaters, Office (Zone B) heaters, Shared-zone heaters, and the Rointe *comfort*/*eco* temperature `number` entities for the hall |
| **Motion sensors** | Hall, office, kitchen, gents and female-toilet motion/presence sensors (`binary_sensor` or `input_boolean`) |
| **Doors & windows** | Per-zone door and window contact groups, shared-zone windows, and the internal door |
| **Calendars, weather & alarm** | Hall & office booking calendars, weather entity, RealFeel sensor, the two alarm booleans, and the water-heater switch |

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
8. **Building empty** → `ice`.

The **shared zone** follows either calendar / any motion / boost, and the
**water heater** turns on for its own pre-heat window, kitchen/toilet motion
(within the keep-alive) or the manual override, and off when the building is
alarmed. Hall comfort/eco setpoints are pushed onto the Rointe `number`
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

## License

MIT — see [LICENSE](LICENSE).
