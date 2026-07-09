# Scout Hut Heating

UI-configurable Home Assistant integration for the Pelsall Scout Hut. Controls
the Rointe hall / office / shared-zone heaters and the kitchen water heater from
booking calendars, motion, door & window sensors, the intruder alarm and the
weather forecast.

- Four-step config flow — map your entities with dropdowns, no YAML.
- Creates its own dashboard controls (pre-heat time, temperatures, boost,
  overrides, ECO keywords…) and diagnostic sensors.
- One reconciler replaces the original ~35 automations with a clear priority:
  manual hold → open door/window → boost → seasonal lockout → alarm → booking →
  motion → empty.

After download, restart HA and add **Scout Hut Heating** from
**Settings → Devices & Services**. See the README for the full entity list and
behaviour notes.
