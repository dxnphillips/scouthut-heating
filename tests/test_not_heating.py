"""A radiator asked to heat whose panel stays cold is alerted (v1.51.4).

Field 2026-11-18: through the 13:30Z pre-heat and the 17:30Z booking hall_back
read 10.0 -> 13.5 for five hours while the other three hall heaters climbed to
~17, all four in comfort at setpoint 19, all uploading. Its panel stayed within
3 °C of its reading (the plausibility rule kept it in for that reason), so it
never fired, and nothing said so.
"""

import custom_components.scout_hut_heating.coordinator as C
from custom_components.scout_hut_heating.const import CONF_HALL_CLIMATES
from scout_testkit import (
    PRESET_COMFORT,
    PRESET_ICE,
    ZA,
    advance,
    make_controller,
    run,
    set_registry,
)

HALL = ["climate.hall_front", "climate.hall_back", "climate.hall_left", "climate.hall_right"]
BACK = "climate.hall_back"


def _surface(c):
    return f"sensor.{c.split('.')[1]}_surface_temperature"


def _events(ctrl, name):
    return [e for e in ctrl.audit.to_list() if e.get("event") == name]


def _hall(back_panel, back_reading=12.0, preset=PRESET_COMFORT):
    ctrl, hass = make_controller({CONF_HALL_CLIMATES: list(HALL)})
    set_registry(
        entries_by_device={f"dev_{c}": [_surface(c)] for c in HALL},
        entity_devices={c: f"dev_{c}" for c in HALL},
    )
    for c in HALL:
        reading = back_reading if c == BACK else 16.5
        panel = back_panel if c == BACK else 60.0
        hass.states.set(c, "heat", {"current_temperature": reading, "temperature": 19.0})
        hass.states.set(_surface(c), str(panel))
    ctrl.applied[ZA] = preset
    return ctrl, hass


def _notes(monkeypatch):
    made, gone = [], []
    monkeypatch.setattr(
        C.persistent_notification, "async_create",
        lambda hass, msg, **k: made.append(k.get("notification_id")),
    )
    monkeypatch.setattr(
        C.persistent_notification, "async_dismiss", lambda hass, nid: gone.append(nid)
    )
    return made, gone


def test_a_cold_panel_on_a_radiator_asked_to_heat_is_alerted(monkeypatch):
    made, gone = _notes(monkeypatch)
    ctrl, hass = _hall(back_panel=13.5)
    run(ctrl._update_heater_not_heating())
    advance(ctrl, C.HEATER_NOT_HEATING_MINUTES - 5)
    run(ctrl._update_heater_not_heating())
    assert not _events(ctrl, "heater_not_heating")
    advance(ctrl, 10)
    run(ctrl._update_heater_not_heating())
    (evt,) = _events(ctrl, "heater_not_heating")
    assert evt["heater"] == BACK and evt["zone"] == ZA and evt["surface"] == 13.5
    assert C.NOTIFY_HEATER_NOT_HEATING in made
    # Once it fires it is heating again.
    hass.states.set(_surface(BACK), "45.0")
    run(ctrl._update_heater_not_heating())
    assert _events(ctrl, "heater_heating")
    assert C.NOTIFY_HEATER_NOT_HEATING in gone


def test_a_hot_panel_is_heating():
    ctrl, _ = _hall(back_panel=58.0)
    run(ctrl._update_heater_not_heating())
    advance(ctrl, C.HEATER_NOT_HEATING_MINUTES + 5)
    run(ctrl._update_heater_not_heating())
    assert not _events(ctrl, "heater_not_heating")


def test_a_radiator_near_its_setpoint_is_not_judged():
    # Inside the Rointe's own finish it may idle with a cool panel.
    ctrl, _ = _hall(back_panel=18.0, back_reading=17.5)
    run(ctrl._update_heater_not_heating())
    advance(ctrl, C.HEATER_NOT_HEATING_MINUTES + 5)
    run(ctrl._update_heater_not_heating())
    assert not _events(ctrl, "heater_not_heating")


def test_a_zone_on_ice_is_not_asking_for_heat():
    ctrl, _ = _hall(back_panel=13.5, preset=PRESET_ICE)
    run(ctrl._update_heater_not_heating())
    advance(ctrl, C.HEATER_NOT_HEATING_MINUTES + 5)
    run(ctrl._update_heater_not_heating())
    assert not _events(ctrl, "heater_not_heating")


def test_a_warm_moment_restarts_the_clock():
    ctrl, hass = _hall(back_panel=13.5)
    run(ctrl._update_heater_not_heating())
    advance(ctrl, 30)
    hass.states.set(_surface(BACK), "40.0")
    run(ctrl._update_heater_not_heating())
    hass.states.set(_surface(BACK), "13.5")
    run(ctrl._update_heater_not_heating())
    advance(ctrl, 30)
    run(ctrl._update_heater_not_heating())
    assert not _events(ctrl, "heater_not_heating")
