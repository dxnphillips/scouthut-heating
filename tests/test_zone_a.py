"""Zone A (Hall) desired-preset priority table."""

from scout_testkit import (
    PRESET_COMFORT,
    PRESET_ECO,
    PRESET_ICE,
    ZA,
    booking,
    boost,
    make_controller,
    motion,
    on,
    E,
)


def test_empty_building_is_ice():
    ctrl, _ = make_controller()
    assert ctrl._desired_zone(ZA) == PRESET_ICE


def test_booking_with_motion_is_comfort():
    ctrl, _ = make_controller()
    booking(ctrl, ZA)
    motion(ctrl, "hall")
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT


def test_booking_without_motion_drops_to_eco():
    ctrl, _ = make_controller()
    booking(ctrl, ZA)
    assert ctrl._desired_zone(ZA) == PRESET_ECO


def test_eco_keyword_booking_stays_eco_even_with_motion():
    ctrl, _ = make_controller()
    booking(ctrl, ZA, "Test event")  # 'test' is a default ECO keyword
    motion(ctrl, "hall")
    assert ctrl._desired_zone(ZA) == PRESET_ECO


def test_opening_ice_forces_ice_over_booking():
    ctrl, _ = make_controller()
    booking(ctrl, ZA)
    motion(ctrl, "hall")
    ctrl.opening_ice[ZA] = True
    assert ctrl._desired_zone(ZA) == PRESET_ICE


def test_boost_beats_seasonal_lockout():
    ctrl, _ = make_controller()
    ctrl.seasonal_lockout = True
    boost(ctrl, ZA)
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT


def test_seasonal_lockout_is_ice():
    ctrl, _ = make_controller()
    ctrl.seasonal_lockout = True
    booking(ctrl, ZA)
    assert ctrl._desired_zone(ZA) == PRESET_ICE


def test_automation_disabled_leaves_alone():
    ctrl, _ = make_controller()
    ctrl._switches["zone_a_automation_enabled"].is_on = False
    booking(ctrl, ZA)
    assert ctrl._desired_zone(ZA) is None


def test_manual_hold_leaves_alone():
    ctrl, _ = make_controller()
    ctrl.manual_hold[ZA] = True
    booking(ctrl, ZA)
    assert ctrl._desired_zone(ZA) is None


def test_alarm_without_booking_is_ice():
    ctrl, hass = make_controller()
    on(hass, E["alarm_main"])
    assert ctrl._desired_zone(ZA) == PRESET_ICE


def test_alarm_during_booking_still_heats():
    ctrl, hass = make_controller()
    on(hass, E["alarm_main"])
    booking(ctrl, ZA)
    motion(ctrl, "hall")
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT


def test_motion_outside_booking_is_eco():
    ctrl, _ = make_controller()
    motion(ctrl, "hall")
    assert ctrl._desired_zone(ZA) == PRESET_ECO


def test_occupied_override_is_eco():
    ctrl, _ = make_controller()
    ctrl._switches["zone_a_occupied_override"].is_on = True
    assert ctrl._desired_zone(ZA) == PRESET_ECO


def test_someone_elsewhere_rests_hall_at_eco():
    # The hall itself is quiet, but the building is not empty: rest at eco
    # rather than leaving a stale (possibly comfort) preset running.
    ctrl, _ = make_controller()
    motion(ctrl, "office")  # not the hall
    assert ctrl._desired_zone(ZA) == PRESET_ECO


def test_preheat_window_holds_comfort_while_empty():
    # The pre-heat window exists to reach the comfort target by event start:
    # the empty-room demotion applies only once the event is running.
    ctrl, _ = make_controller()
    ctrl.cal_window[ZA] = True  # event within pre-heat window, not yet started
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT


def test_alarm_clears_the_occupied_override():
    # Original A33/A34: arming with no booking cancels a lingering override,
    # or it would silently resume heating the empty zone at disarm.
    ctrl, hass = make_controller()
    ctrl._switches["zone_a_occupied_override"].is_on = True
    on(hass, E["alarm_main"])
    assert ctrl._desired_zone(ZA) == PRESET_ICE
    assert ctrl.switch_on("zone_a_occupied_override") is False
