"""Zone B (Office) desired-preset priority table and independence from Zone A."""

from scout_testkit import (
    PRESET_COMFORT,
    PRESET_ECO,
    PRESET_ICE,
    ZA,
    ZB,
    booking,
    boost,
    make_controller,
    motion,
    on,
    E,
)


def test_empty_is_ice():
    ctrl, _ = make_controller()
    assert ctrl._desired_zone(ZB) == PRESET_ICE


def test_booking_with_motion_is_comfort():
    ctrl, _ = make_controller()
    booking(ctrl, ZB)
    motion(ctrl, "office")
    assert ctrl._desired_zone(ZB) == PRESET_COMFORT


def test_booking_without_motion_is_eco():
    ctrl, _ = make_controller()
    booking(ctrl, ZB)
    assert ctrl._desired_zone(ZB) == PRESET_ECO


def test_opening_ice_is_ice():
    ctrl, _ = make_controller()
    ctrl.opening_ice[ZB] = True
    booking(ctrl, ZB)
    assert ctrl._desired_zone(ZB) == PRESET_ICE


def test_boost_is_comfort():
    ctrl, _ = make_controller()
    boost(ctrl, ZB)
    assert ctrl._desired_zone(ZB) == PRESET_COMFORT


def test_lockout_is_ice():
    ctrl, _ = make_controller()
    ctrl.seasonal_lockout = True
    assert ctrl._desired_zone(ZB) == PRESET_ICE


def test_automation_disabled_leaves_alone():
    ctrl, _ = make_controller()
    ctrl._switches["zone_b_automation_enabled"].is_on = False
    booking(ctrl, ZB)
    assert ctrl._desired_zone(ZB) is None


def test_manual_hold_leaves_alone():
    ctrl, _ = make_controller()
    ctrl.manual_hold[ZB] = True
    assert ctrl._desired_zone(ZB) is None


def test_alarm_office_without_booking_is_ice():
    ctrl, hass = make_controller()
    on(hass, E["alarm_office"])
    assert ctrl._desired_zone(ZB) == PRESET_ICE


def test_office_motion_outside_booking_is_eco():
    ctrl, _ = make_controller()
    motion(ctrl, "office")
    assert ctrl._desired_zone(ZB) == PRESET_ECO


def test_occupied_override_is_eco():
    ctrl, _ = make_controller()
    ctrl._switches["zone_b_occupied_override"].is_on = True
    assert ctrl._desired_zone(ZB) == PRESET_ECO


def test_zone_a_alarm_does_not_ice_zone_b():
    # Zone A alarm is set, but Zone B has its own occupancy and no Zone B alarm:
    # the two zones are independent.
    ctrl, hass = make_controller()
    on(hass, E["alarm_main"])
    motion(ctrl, "office")
    assert ctrl._desired_zone(ZB) == PRESET_ECO
    assert ctrl._desired_zone(ZA) == PRESET_ICE
