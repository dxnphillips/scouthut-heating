"""Fakes and a controller builder shared by the scenario tests."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from homeassistant.helpers import entity_registry as er

from custom_components.scout_hut_heating import coordinator as C
from custom_components.scout_hut_heating.const import (
    CONF_ALARM_MAIN,
    CONF_ALARM_OFFICE,
    CONF_CALENDAR_HALL,
    CONF_CALENDAR_OFFICE,
    CONF_HALL_CLIMATES,
    CONF_INTERNAL_DOOR,
    CONF_MOTION_FEMALE,
    CONF_MOTION_GENTS,
    CONF_MOTION_HALL,
    CONF_MOTION_KITCHEN,
    CONF_MOTION_OFFICE,
    CONF_OFFICE_CLIMATES,
    CONF_REALFEEL,
    CONF_SHARED_CLIMATES,
    CONF_SHARED_WINDOWS,
    CONF_WATER_SWITCH,
    CONF_WEATHER,
    CONF_ZONE_A_DOORS,
    CONF_ZONE_A_WINDOWS,
    CONF_ZONE_B_DOORS,
    CONF_ZONE_B_WINDOWS,
    DEFAULT_ECO_KEYWORDS,
    NUMBER_DEFS,
    SWITCH_DEFS,
    ZONE_A,
    ZONE_B,
)

# Re-export so tests can `from scout_testkit import ZONE_A, PRESET_ICE, ...`
PRESET_ICE = C.PRESET_ICE
PRESET_ECO = C.PRESET_ECO
PRESET_COMFORT = C.PRESET_COMFORT
ZA = ZONE_A
ZB = ZONE_B

# Stable entity ids wired into the default (fully configured) controller.
E = {
    "hall": ["climate.hall_back", "climate.hall_front"],
    "office": ["climate.office"],
    "shared": ["climate.kitchen", "climate.gents", "climate.ladies"],
    "cal_hall": "calendar.hall",
    "cal_office": "calendar.office",
    "alarm_main": "input_boolean.alarm_main",
    "alarm_office": "input_boolean.alarm_office",
    "water": "switch.water",
    "m_hall": "binary_sensor.m_hall",
    "m_office": "binary_sensor.m_office",
    "m_kitchen": "binary_sensor.m_kitchen",
    "m_gents": "binary_sensor.m_gents",
    "m_female": "binary_sensor.m_female",
    "a_door": "binary_sensor.hall_door",
    "a_window": "binary_sensor.hall_window",
    "b_door": "binary_sensor.office_door",
    "b_window": "binary_sensor.office_window",
    "shared_window": "binary_sensor.kitchen_window",
    "internal": "binary_sensor.internal_door",
    "weather": "weather.forecast",
    "realfeel": "sensor.realfeel",
}


class FakeState:
    def __init__(self, state, attrs=None):
        self.state = state
        self.attributes = attrs or {}


class FakeStates:
    def __init__(self):
        self._d = {}

    def get(self, entity_id):
        return self._d.get(entity_id)

    def set(self, entity_id, state, attrs=None):
        self._d[entity_id] = FakeState(state, attrs)


class FakeServices:
    def __init__(self):
        self.calls = []

    async def async_call(
        self, domain, service, data=None, blocking=False, target=None, return_response=False, **kw
    ):
        self.calls.append({"domain": domain, "service": service, "data": data or {}, "target": target})
        return {} if return_response else None


class FakeHass:
    def __init__(self):
        self.states = FakeStates()
        self.services = FakeServices()
        self.data = {}


class FakeNum:
    def __init__(self, value):
        self.native_value = value
        self._default = value

    def restore_default(self):
        self.native_value = self._default

    def write_value(self, value):
        self.native_value = value


class FakeSwitch:
    def __init__(self, is_on):
        self.is_on = is_on
        self._default = is_on

    def restore_default(self):
        self.is_on = self._default


class FakeText:
    def __init__(self, value):
        self.native_value = value
        self._default = value

    def restore_default(self):
        self.native_value = self._default


class FakeSelect:
    def __init__(self, option):
        self.current_option = option
        self._default = option

    def restore_default(self):
        self.current_option = self._default


def default_config() -> dict:
    return {
        CONF_HALL_CLIMATES: list(E["hall"]),
        CONF_OFFICE_CLIMATES: list(E["office"]),
        CONF_SHARED_CLIMATES: list(E["shared"]),
        CONF_CALENDAR_HALL: E["cal_hall"],
        CONF_CALENDAR_OFFICE: E["cal_office"],
        CONF_ALARM_MAIN: E["alarm_main"],
        CONF_ALARM_OFFICE: E["alarm_office"],
        CONF_WATER_SWITCH: E["water"],
        CONF_MOTION_HALL: E["m_hall"],
        CONF_MOTION_OFFICE: E["m_office"],
        CONF_MOTION_KITCHEN: E["m_kitchen"],
        CONF_MOTION_GENTS: E["m_gents"],
        CONF_MOTION_FEMALE: E["m_female"],
        CONF_ZONE_A_DOORS: [E["a_door"]],
        CONF_ZONE_A_WINDOWS: [E["a_window"]],
        CONF_ZONE_B_DOORS: [E["b_door"]],
        CONF_ZONE_B_WINDOWS: [E["b_window"]],
        CONF_SHARED_WINDOWS: [E["shared_window"]],
        CONF_INTERNAL_DOOR: E["internal"],
        CONF_WEATHER: E["weather"],
        CONF_REALFEEL: E["realfeel"],
    }


def make_controller(config_overrides=None, started=True):
    """Build a fully-configured controller with default tunables and all inputs off."""
    hass = FakeHass()
    cfg = default_config()
    if config_overrides:
        cfg.update(config_overrides)
    ctrl = C.ScoutController(hass, C.ConfigEntry(data=cfg))

    for key, defn in NUMBER_DEFS.items():
        ctrl.register_number(key, FakeNum(defn[3]))
    for key, state in SWITCH_DEFS.items():
        ctrl.register_switch(key, FakeSwitch(state))
    ctrl.register_text("eco_keywords", FakeText(DEFAULT_ECO_KEYWORDS))
    ctrl.register_select("boost_duration", FakeSelect("60 min"))

    # Everything closed / off by default.
    for eid in (
        E["cal_hall"], E["cal_office"], E["alarm_main"], E["alarm_office"],
        E["m_hall"], E["m_office"], E["m_kitchen"], E["m_gents"], E["m_female"],
        E["a_door"], E["a_window"], E["b_door"], E["b_window"],
        E["shared_window"], E["internal"], E["water"],
    ):
        hass.states.set(eid, "off")

    ctrl._started = started
    return ctrl, hass


# ---------------------------------------------------------------------------
# Small helpers used by the scenarios
# ---------------------------------------------------------------------------
def on(hass, entity_id, attrs=None):
    hass.states.set(entity_id, "on", attrs)


def off(hass, entity_id, attrs=None):
    hass.states.set(entity_id, "off", attrs)


def advance(ctrl, minutes):
    """Simulate `minutes` of elapsed time by rewinding every stored timestamp.

    Moving the remembered instants further into the past is equivalent to the
    clock moving forward, which lets sequence tests age motion, open-since,
    boost and last-apply timers deterministically without real waits.
    """
    delta = timedelta(minutes=minutes)
    for area, ts in ctrl.last_motion.items():
        if ts is not None:
            ctrl.last_motion[area] = ts - delta
    for key, ts in ctrl.open_since.items():
        if ts is not None:
            ctrl.open_since[key] = ts - delta
    for zone, ts in ctrl.boost_until.items():
        if ts is not None:
            ctrl.boost_until[zone] = ts - delta
    for zone in list(ctrl._last_apply):
        ctrl._last_apply[zone] = ctrl._last_apply[zone] - delta
    for zone, sample in ctrl._warmup_start.items():
        if sample is not None:
            ctrl._warmup_start[zone] = (sample[0] - delta, sample[1])
    if ctrl.water_on_since is not None:
        ctrl.water_on_since = ctrl.water_on_since - delta
    if ctrl.water_last_hot is not None:
        ctrl.water_last_hot = ctrl.water_last_hot - delta
    if ctrl.water_hygiene_until is not None:
        ctrl.water_hygiene_until = ctrl.water_hygiene_until - delta


def set_preset_state(hass, entity_id, preset):
    """Simulate a heater currently reporting a preset (e.g. changed in the app)."""
    hass.states.set(entity_id, "heat", {"preset_mode": preset})


def booking(ctrl, zone, title=""):
    """Simulate an active booking / pre-heat window for a zone."""
    ctrl.cal_window[zone] = True
    ctrl.cal_title[zone] = title.lower()


def motion(ctrl, area):
    """Record recent motion in an area (hall/office/kitchen/gents/female)."""
    ctrl.last_motion[area] = ctrl._now()


def boost(ctrl, zone, minutes=30):
    ctrl.boost_until[zone] = ctrl._now() + timedelta(minutes=minutes)


def run(coro):
    return asyncio.run(coro)


def service_calls(hass, domain, service):
    return [c for c in hass.services.calls if c["domain"] == domain and c["service"] == service]


def preset_for(hass, entity_id):
    """Last preset applied to a climate entity via climate.set_preset_mode."""
    result = None
    for c in service_calls(hass, "climate", "set_preset_mode"):
        ids = c["data"].get("entity_id")
        ids = ids if isinstance(ids, list) else [ids]
        if entity_id in ids:
            result = c["data"].get("preset_mode")
    return result


def set_registry(entries_by_device, entity_devices):
    """Populate the entity-registry stub for auto-discovery tests."""
    reg = er._REG
    reg.by_id = {
        eid: er._RegEntry(eid, dev) for eid, dev in entity_devices.items()
    }
    reg.by_device = {
        dev: [er._RegEntry(e, dev) for e in ents]
        for dev, ents in entries_by_device.items()
    }
