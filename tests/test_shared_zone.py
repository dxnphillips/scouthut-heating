"""Shared zone (toilets + kitchen) desired-preset logic."""

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
from custom_components.scout_hut_heating.const import CONF_SHARED_CLIMATES


def test_no_shared_climates_returns_none():
    ctrl, _ = make_controller({CONF_SHARED_CLIMATES: []})
    assert ctrl._desired_shared() is None


def test_empty_is_ice():
    ctrl, _ = make_controller()
    assert ctrl._desired_shared() == PRESET_ICE


def test_hall_booking_makes_shared_eco():
    ctrl, _ = make_controller()
    booking(ctrl, ZA)
    assert ctrl._desired_shared() == PRESET_ECO


def test_office_booking_makes_shared_eco():
    ctrl, _ = make_controller()
    booking(ctrl, ZB)
    assert ctrl._desired_shared() == PRESET_ECO


def test_boost_a_makes_shared_comfort():
    ctrl, _ = make_controller()
    boost(ctrl, ZA)
    assert ctrl._desired_shared() == PRESET_COMFORT


def test_boost_b_makes_shared_comfort():
    ctrl, _ = make_controller()
    boost(ctrl, ZB)
    assert ctrl._desired_shared() == PRESET_COMFORT


def test_both_alarms_make_shared_ice():
    ctrl, hass = make_controller()
    on(hass, E["alarm_main"])
    on(hass, E["alarm_office"])
    motion(ctrl, "kitchen")  # even with motion, a locked building is ice
    assert ctrl._desired_shared() == PRESET_ICE


def test_single_alarm_does_not_ice_shared():
    ctrl, hass = make_controller()
    on(hass, E["alarm_main"])
    motion(ctrl, "kitchen")
    assert ctrl._desired_shared() == PRESET_ECO


def test_motion_anywhere_makes_shared_eco():
    ctrl, _ = make_controller()
    motion(ctrl, "gents")
    assert ctrl._desired_shared() == PRESET_ECO


def test_seasonal_lockout_is_ice():
    ctrl, _ = make_controller()
    ctrl.seasonal_lockout = True
    booking(ctrl, ZA)
    assert ctrl._desired_shared() == PRESET_ICE


def test_shared_opening_ice_is_ice():
    ctrl, _ = make_controller()
    booking(ctrl, ZA)
    ctrl.opening_ice["shared"] = True
    assert ctrl._desired_shared() == PRESET_ICE


def test_boost_beats_both_alarms():
    ctrl, hass = make_controller()
    on(hass, E["alarm_main"])
    on(hass, E["alarm_office"])
    boost(ctrl, ZA)
    assert ctrl._desired_shared() == PRESET_COMFORT
