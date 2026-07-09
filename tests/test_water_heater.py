"""Water heater desired-state logic."""

from scout_testkit import make_controller, motion, on, E
from custom_components.scout_hut_heating.const import CONF_WATER_SWITCH


def test_no_switch_returns_none():
    ctrl, _ = make_controller({CONF_WATER_SWITCH: None})
    assert ctrl._desired_water() is None


def test_empty_is_off():
    ctrl, _ = make_controller()
    assert ctrl._desired_water() is False


def test_manual_override_is_on():
    ctrl, _ = make_controller()
    ctrl._switches["water_manual_override"].is_on = True
    assert ctrl._desired_water() is True


def test_calendar_window_is_on():
    ctrl, _ = make_controller()
    ctrl.water_window = True
    assert ctrl._desired_water() is True


def test_kitchen_motion_is_on():
    ctrl, _ = make_controller()
    motion(ctrl, "kitchen")
    assert ctrl._desired_water() is True


def test_gents_motion_is_on():
    ctrl, _ = make_controller()
    motion(ctrl, "gents")
    assert ctrl._desired_water() is True


def test_female_motion_is_on():
    ctrl, _ = make_controller()
    motion(ctrl, "female")
    assert ctrl._desired_water() is True


def test_hall_motion_does_not_keep_water_on():
    # The water heater only follows kitchen / toilet motion, not the hall/office.
    ctrl, _ = make_controller()
    motion(ctrl, "hall")
    motion(ctrl, "office")
    assert ctrl._desired_water() is False


def test_both_alarms_suppress_motion():
    ctrl, hass = make_controller()
    on(hass, E["alarm_main"])
    on(hass, E["alarm_office"])
    motion(ctrl, "kitchen")
    assert ctrl._desired_water() is False


def test_both_alarms_but_override_is_on():
    ctrl, hass = make_controller()
    on(hass, E["alarm_main"])
    on(hass, E["alarm_office"])
    ctrl._switches["water_manual_override"].is_on = True
    assert ctrl._desired_water() is True


def test_both_alarms_but_calendar_is_on():
    ctrl, hass = make_controller()
    on(hass, E["alarm_main"])
    on(hass, E["alarm_office"])
    ctrl.water_window = True
    assert ctrl._desired_water() is True


def test_single_alarm_does_not_suppress_motion():
    ctrl, hass = make_controller()
    on(hass, E["alarm_main"])
    motion(ctrl, "kitchen")
    assert ctrl._desired_water() is True
