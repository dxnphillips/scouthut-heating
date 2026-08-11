"""A transient Rointe reading drop-out must not flip a warm room to 'wants heat'.

Field 2026-08-11: a ~17 s hall-probe blip on a hot afternoon made
`_room_wants_heat` err warm -> the genuinely warm hall flipped ice->comfort->ice,
reversing the cooling fans once per blip. Two layers now guard it: (A) hold the
room's own recent reading through a brief blip; (C) on a sustained loss, consult
the independent ceiling + outdoor before erring warm.
"""

from scout_testkit import ZA, E, advance, hall_temp, make_controller
from custom_components.scout_hut_heating.const import CONF_CEILING_TEMP


def _unavailable_hall(ctrl):
    for eid in E["hall"]:
        ctrl.hass.states.set(eid, "unavailable")


def _target(ctrl):
    return ctrl._zone_target(ZA)


def test_transient_blip_holds_the_recent_warm_reading():
    # The bug: a warm hall read as warm, then the probe blipped -> err warm.
    ctrl, _ = make_controller()
    hall_temp(ctrl, 21.0)  # warm -> read and cached
    assert ctrl._room_wants_heat(ZA, _target(ctrl)) is False
    _unavailable_hall(ctrl)  # cloud blips out
    assert ctrl._room_wants_heat(ZA, _target(ctrl)) is False  # recent truth held


def test_transient_blip_holds_a_recent_cold_reading_too():
    # Symmetric: a cold hall stays wanting heat through the blip (no lost heat).
    ctrl, _ = make_controller()
    hall_temp(ctrl, 15.0)
    assert ctrl._room_wants_heat(ZA, _target(ctrl)) is True
    _unavailable_hall(ctrl)
    assert ctrl._room_wants_heat(ZA, _target(ctrl)) is True


def test_sustained_loss_without_evidence_errs_warm():
    ctrl, _ = make_controller()
    hall_temp(ctrl, 21.0)
    ctrl._room_wants_heat(ZA, _target(ctrl))  # cache the good reading
    _unavailable_hall(ctrl)
    advance(ctrl, 5)  # past the 2-min hold grace, no ceiling/outdoor mapped
    assert ctrl._room_wants_heat(ZA, _target(ctrl)) is True  # fail-safe


def test_sustained_loss_ceiling_and_outdoor_confirm_warm():
    # The reinforcement: hot ceiling AND hot outside -> the building is genuinely
    # warm, no cold floor possible -> withhold heat even past the hold grace.
    ctrl, hass = make_controller(config_overrides={CONF_CEILING_TEMP: "sensor.ceiling"})
    hall_temp(ctrl, 21.0)
    ctrl._room_wants_heat(ZA, _target(ctrl))
    _unavailable_hall(ctrl)
    hass.states.set("sensor.ceiling", "29.0")
    hass.states.set("weather.forecast", "sunny", {"temperature": 25.0})
    advance(ctrl, 5)
    assert ctrl._room_wants_heat(ZA, _target(ctrl)) is False


def test_sustained_loss_hot_ceiling_but_cold_outdoor_errs_warm():
    # Winter stratification safety: a warm ceiling can sit over a cold floor when
    # it is cold outside (residual roof heat) -> the ceiling alone must NOT
    # withhold heat.
    ctrl, hass = make_controller(config_overrides={CONF_CEILING_TEMP: "sensor.ceiling"})
    hall_temp(ctrl, 21.0)
    ctrl._room_wants_heat(ZA, _target(ctrl))
    _unavailable_hall(ctrl)
    hass.states.set("sensor.ceiling", "24.0")  # warm ceiling
    hass.states.set("weather.forecast", "cloudy", {"temperature": 5.0})  # cold out
    advance(ctrl, 5)
    assert ctrl._room_wants_heat(ZA, _target(ctrl)) is True  # err warm (safe)
