"""One-press creation of the Scout Hut dashboards.

Generates the same dashboards as docs/heating_dashboard.yaml and
docs/fan_dashboard.yaml, but with the REAL entity ids resolved from the
entity registry (the integration's own helpers) and from the config entry
(the mapped Rointe / Shelly entities), so nothing needs hand-editing.

The Lovelace storage API is semi-internal and has been reshaped across Home
Assistant releases, so everything HA-facing here is feature-detected and
fails soft: the caller surfaces any error as a notification pointing at the
YAML files in docs/, which remain the manual fallback.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

DASHBOARD_URL = "scout-hut"
DASHBOARD_TITLE = "Scout Hut"
DASHBOARD_ICON = "mdi:campfire"

# Returned instead of an error when the dashboard was created/updated in
# storage but the running Lovelace could not be told about it live (modern HA
# keeps its dashboards collection private): a restart will surface it.
RESTART_REQUIRED = "__restart_required__"

# (helper key, display name) rows per card. A row is silently dropped when the
# helper is missing from the registry, so the dashboard is always valid.
_NOW = [
    ("zone_a_status", "Hall preset"),
    ("zone_b_status", "Office preset"),
    ("shared_status", "Shared zone preset"),
    ("water_status", "Water heater"),
    ("seasonal_lockout_active", "Seasonal lockout"),
    ("hall_temp_spread", "Hall temperature spread"),
    ("fan_delta_t", "Ceiling-floor ΔT"),
]
_BOOST = [
    ("boost_zone_a", "Boost hall"),
    ("boost_zone_b", "Boost office"),
    ("cancel_boost_zone_a", "Cancel hall boost"),
    ("cancel_boost_zone_b", "Cancel office boost"),
    ("boost_duration", "Boost duration"),
    ("zone_a_boost_active", "Hall boost active"),
    ("zone_b_boost_active", "Office boost active"),
]
_TEMPERATURES = [
    ("hall_comfort_temp", "Hall comfort"),
    ("hall_eco_temp", "Hall eco"),
    ("hall_eco_low_temp", "Hall eco-low (ECO bookings)"),
    ("seasonal_lockout_temp", "Seasonal lockout threshold"),
]
_OCCUPANCY = [
    ("zone_a_automation_enabled", "Hall automation"),
    ("zone_b_automation_enabled", "Office automation"),
    ("zone_a_occupied_override", "Hall occupied override"),
    ("zone_b_occupied_override", "Office occupied override"),
    ("motion_timeout_minutes", "No-motion eco timeout"),
    ("zone_a_manual_hold", "Hall manual hold (app change)"),
    ("zone_b_manual_hold", "Office manual hold (app change)"),
]
_OPTIMUM_START = [
    ("preheat_minutes", "Pre-heat lead time (max / cap)"),
    ("zone_a_warmup_rate", "Hall warm-up rate"),
    ("zone_a_warmup_rate_fans", "Hall warm-up rate (fans running)"),
    ("zone_b_warmup_rate", "Office warm-up rate"),
    ("zone_a_heatloss_pct", "Hall heat loss %/h"),
    ("zone_b_heatloss_pct", "Office heat loss %/h"),
]
_WATER = [
    ("water_manual_override", "Manual override"),
    ("water_preheat_minutes", "Pre-heat lead time"),
    ("water_motion_keepalive_minutes", "Keep on after motion"),
]
_BOOKINGS = [
    ("zone_a_expected_preset", "Hall expected preset"),
    ("zone_b_expected_preset", "Office expected preset"),
    ("eco_keywords", "ECO keyword blocklist"),
]
_HOUSEKEEPING = [
    ("zone_a_opening_ice_active", "Hall opening ice"),
    ("zone_b_opening_ice_active", "Office opening ice"),
    ("shared_opening_ice_active", "Shared opening ice"),
    ("door_ice_minutes", "Door ice delay"),
    ("window_ice_minutes", "Window ice delay"),
    ("reset_tunables", "Reset tunables to defaults"),
    ("create_dashboards", "Recreate these dashboards"),
]
_FAN_STATUS = [
    ("fan_running", "Fans running"),
    ("fan_mode", "Mode"),
    ("fan_direction", "Direction"),
    ("fan_delta_t", "Ceiling-floor ΔT"),
    ("fan_heat_demand", "Heat demand active"),
    ("fan_fault_effective", "Fault"),
    ("fan_sensor_stale", "Sensor lost"),
]
_FAN_CONTROLS = [
    ("fans_enabled", "Ceiling fans enabled"),
    ("summer_mode", "Summer cooling mode (manual force)"),
    ("summer_follows_season", "Summer cooling follows season"),
    ("fans_run_on_sensor_loss", "Run when sensor lost"),
    ("winter_fans_need_occupancy", "Winter fans need occupancy"),
]
_FAN_TUNING = [
    ("fan_dt_on", "ΔT to start"),
    ("fan_dt_off", "ΔT to stop"),
    ("fan_min_run_minutes", "Minimum run time"),
    ("fan_min_off_minutes", "Minimum off time"),
    ("fan_sensor_stale_minutes", "Sensor stale after"),
    ("cooling_temp_high", "Cooling warm-enough temperature"),
    ("cooling_mix_max_temp", "Max useful breeze temperature"),
    ("heat_demand_watts", "Heat-demand power threshold"),
    ("fan_recirc_max_floor_temp", "Recirculate until floor reaches"),
]
# Mapped hardware shown on the fans view: (config key, display name).
_FAN_HARDWARE = [
    ("ceiling_temp", "Ceiling temperature"),
    ("fan_o1_power", "O1 power (transformer + fans)"),
    ("fan_o2_power", "O2 power (direction coil)"),
    ("fan_master", "Master (O1)"),
    ("fan_direction", "Direction relay (O2) — open=forward, closed=reverse"),
    ("fan_reverse", "Reverse fans (safe sequence)"),
]


def _rows(emap: dict[str, str], spec: list[tuple[str, str]]) -> list[dict[str, str]]:
    return [{"entity": emap[key], "name": name} for key, name in spec if key in emap]


def _card(title: str, rows: list[dict[str, str]]) -> dict[str, Any] | None:
    return {"type": "entities", "title": title, "entities": rows} if rows else None


def build_config(emap: dict[str, str], mapped: dict[str, Any]) -> dict[str, Any]:
    """Build the full dashboard config from resolved entity ids.

    emap: integration helper key -> entity_id (from the entity registry).
    mapped: the config entry's entity mappings (Rointe climates, Shelly, ...).
    """
    heating_cards = [
        c
        for c in (
            _card("Now", _rows(emap, _NOW)),
            _card("Boost", _rows(emap, _BOOST)),
            _card("Temperatures", _rows(emap, _TEMPERATURES)),
            _card("Occupancy & overrides", _rows(emap, _OCCUPANCY)),
            _card("Optimum start (learned)", _rows(emap, _OPTIMUM_START)),
            _card("Water heater", _rows(emap, _WATER)),
            _card("Bookings", _rows(emap, _BOOKINGS)),
            _card("Openings & housekeeping", _rows(emap, _HOUSEKEEPING)),
        )
        if c is not None
    ]

    climates: list[str] = []
    for key in ("hall_climates", "office_climates", "shared_climates"):
        value = mapped.get(key) or []
        climates.extend([value] if isinstance(value, str) else list(value))
    if climates:
        heating_cards.append(
            {"type": "entities", "title": "Radiators (Rointe)", "entities": climates}
        )

    graph = [
        {"entity": emap[key], "name": name}
        for key, name in (
            ("hall_temp_spread", "Hall spread"),
            ("fan_delta_t", "Ceiling-floor ΔT"),
        )
        if key in emap
    ]
    if graph:
        heating_cards.append(
            {
                "type": "history-graph",
                "title": "Mixing instruments (24 h)",
                "hours_to_show": 24,
                "entities": graph,
            }
        )

    fan_cards = [
        c
        for c in (
            _card("Status", _rows(emap, _FAN_STATUS)),
            _card("Controls", _rows(emap, _FAN_CONTROLS)),
            _card("Tuning", _rows(emap, _FAN_TUNING)),
        )
        if c is not None
    ]
    hardware = [
        {"entity": mapped[key], "name": name}
        for key, name in _FAN_HARDWARE
        if mapped.get(key)
    ]
    if hardware:
        fan_cards.append(
            {"type": "entities", "title": "Shelly (manual / diagnostics)", "entities": hardware}
        )

    views = [{"title": "Heating", "path": "heating", "cards": heating_cards}]
    if fan_cards:
        views.append({"title": "Fans", "path": "fans", "cards": fan_cards})
    return {"views": views}


def _entity_map(hass: HomeAssistant, entry_id: str) -> dict[str, str]:
    """Map each integration helper key to its real entity id."""
    registry = er.async_get(hass)
    prefix = f"{entry_id}_"
    emap: dict[str, str] = {}
    for entry in er.async_entries_for_config_entry(registry, entry_id):
        unique_id = getattr(entry, "unique_id", None) or ""
        if unique_id.startswith(prefix):
            emap[unique_id[len(prefix) :]] = entry.entity_id
    return emap


async def async_create_or_update(hass: HomeAssistant, controller: Any) -> str | None:
    """Create (or refresh) the Scout Hut dashboard. Returns an error string.

    The Lovelace storage API is not a stable public surface, so every access
    is feature-detected across the dict (older HA) and dataclass (newer HA)
    shapes of hass.data["lovelace"]. Any missing piece returns a message and
    the caller falls back to pointing at the YAML files in docs/.
    """
    config = build_config(
        _entity_map(hass, controller.entry.entry_id), controller.config
    )

    lovelace = hass.data.get("lovelace")
    if lovelace is None:
        return "the Lovelace integration is not loaded"

    def _get(name: str) -> Any:
        value = getattr(lovelace, name, None)
        if value is None and isinstance(lovelace, dict):
            value = lovelace.get(name)
        return value

    dashboards = _get("dashboards")
    if dashboards is None:
        return "this Home Assistant version does not expose the dashboard store"

    created_offline = False
    item: dict[str, Any] | None = None
    if DASHBOARD_URL not in dashboards:
        collection = _get("dashboards_collection")
        if collection is None:
            # Modern HA (2025.2+) keeps the running collection private. Load
            # our own instance over the same storage: the item persists and
            # the sidebar picks it up on the next restart.
            from homeassistant.components.lovelace import dashboard as lovelace_dashboard

            collection = lovelace_dashboard.DashboardsCollection(hass)
            await collection.async_load()
            created_offline = True
        existing = [
            entry
            for entry in collection.async_items()
            if entry.get("url_path") == DASHBOARD_URL
        ]
        if existing:
            item = existing[0]
        else:
            item = await collection.async_create_item(
                {
                    "url_path": DASHBOARD_URL,
                    "title": DASHBOARD_TITLE,
                    "icon": DASHBOARD_ICON,
                    "show_in_sidebar": True,
                    "require_admin": False,
                }
            )

    dashboard = dashboards.get(DASHBOARD_URL)
    if dashboard is None and item is not None:
        # Not registered with the running Lovelace: write the config straight
        # to the dashboard's own store so it is ready when the panel appears.
        from homeassistant.components.lovelace import dashboard as lovelace_dashboard

        dashboard = lovelace_dashboard.LovelaceStorage(hass, item)
    if dashboard is None:
        return "the dashboard was created but did not register"
    await dashboard.async_save(config)
    return RESTART_REQUIRED if created_offline else None
