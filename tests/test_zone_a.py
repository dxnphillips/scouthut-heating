"""Zone A (Hall) desired-preset priority table."""

from scout_testkit import (
    PRESET_COMFORT,
    PRESET_ECO,
    PRESET_ICE,
    ZA,
    booking,
    boost,
    hall_temp,
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


def test_season_no_longer_ices_a_cold_booking():
    # The season no longer gates heating: a cold booking heats whatever the
    # calendar says (the old lockout would have forced ice here).
    ctrl, _ = make_controller()
    ctrl.seasonal_lockout = True
    booking(ctrl, ZA)
    motion(ctrl, "hall")
    hall_temp(ctrl, 12.0)  # genuinely cold
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT


def test_warm_booking_ices_for_the_cooling_fans():
    # A booking already at/above target needs no heat; ice frees the cooling fans.
    ctrl, hass = make_controller()
    hass.states.set("weather.forecast", "sunny", {"temperature": 22.0})  # warm -> no hold
    booking(ctrl, ZA)
    motion(ctrl, "hall")
    hall_temp(ctrl, 20.5)  # above the 19.5 comfort target
    assert ctrl._desired_zone(ZA) == PRESET_ICE
    assert ctrl._preset_reason[ZA] == "booking_warm"


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


def test_motion_in_a_cold_hall_heats_to_comfort():
    # Unified with a booking: bare presence in a genuinely cold hall heats to the
    # SAME comfort target (was eco 16 before the occupied/booked split was removed).
    ctrl, _ = make_controller()
    motion(ctrl, "hall")
    hall_temp(ctrl, 15.0)
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT
    assert ctrl._preset_reason[ZA] == "motion"


def test_motion_in_a_warm_hall_ices_for_the_cooling_fans():
    ctrl, _ = make_controller()
    motion(ctrl, "hall")
    hall_temp(ctrl, 21.0)  # already warm — no heat, let the fans cool
    assert ctrl._desired_zone(ZA) == PRESET_ICE
    assert ctrl._preset_reason[ZA] == "occupied_warm"


def test_occupied_override_in_a_cold_hall_heats_to_comfort():
    ctrl, _ = make_controller()
    ctrl._switches["zone_a_occupied_override"].is_on = True
    hall_temp(ctrl, 15.0)
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT
    assert ctrl._preset_reason[ZA] == "occupied_override"


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
