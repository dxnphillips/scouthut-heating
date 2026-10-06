"""The eco tail an ECO-keyword booking leaves behind (v1.49.0).

Field 2026-10-02: two sal-vation cleaning bookings (eco-low target 14) ended at
06:00Z and 10:15Z with the cleaner still moving about the hall, and on each end
edge the bare-occupancy rung lit all four hall heaters toward comfort 19 on a
15–17 °C hall the booking had just said should sit at 14 — 6–10 minutes of
firing each time until the alarm iced them, plus panels at 35 °C warming an
empty hall afterwards. The person still tripping the PIR after a low-key booking
is that booking's visit, so for one motion timeout after an eco booking ends the
zone heats toward the eco-low target it was given, not comfort.
"""

from scout_testkit import (
    PRESET_COMFORT,
    PRESET_ECO,
    PRESET_ICE,
    ZA,
    advance,
    booking,
    end_booking,
    hall_temp,
    make_controller,
    motion,
)


def _ended_eco_booking(temp):
    ctrl, hass = make_controller()
    hass.states.set("weather.forecast", "cloudy", {"temperature": 12.0})
    ctrl._numbers["hall_comfort_temp"].native_value = 19.0
    hall_temp(ctrl, temp)
    ctrl._record_booking_edges()  # baseline: calendar off
    booking(ctrl, ZA, "sal-vation cleaning")
    ctrl._record_booking_edges()  # start edge: eco booking latched
    end_booking(ctrl, ZA)
    ctrl._record_booking_edges()  # end edge: the tail begins
    ctrl.cal_title[ZA] = ""  # the look-ahead refresh the end edge flags clears it
    return ctrl


def test_the_cleaner_finishing_up_does_not_light_comfort():
    # 2026-10-02 06:00Z: hall 15.5, eco-low 14 -> above target, no heat at all.
    ctrl = _ended_eco_booking(15.5)
    motion(ctrl, "hall")
    assert ctrl._desired_zone(ZA) == PRESET_ICE
    assert ctrl._preset_reason[ZA] == "occupied_warm"


def test_a_cold_hall_in_the_tail_heats_toward_eco_low_not_comfort():
    ctrl = _ended_eco_booking(12.0)
    motion(ctrl, "hall")
    assert ctrl._desired_zone(ZA) == PRESET_ECO
    assert ctrl._preset_reason[ZA] == "eco_tail"
    # ...and the eco preset carries the eco-LOW setpoint while the tail runs.
    assert ctrl._eco_low_wanted(ZA) is True
    assert ctrl._hall_eco_target(ctrl._eco_low_wanted(ZA)) == ctrl.number("hall_eco_low_temp")


def test_the_tail_lasts_one_motion_timeout_then_occupancy_is_ordinary_again():
    ctrl = _ended_eco_booking(15.5)
    motion(ctrl, "hall")
    assert ctrl._desired_zone(ZA) == PRESET_ICE
    advance(ctrl, ctrl.number("motion_timeout_minutes") + 1)
    motion(ctrl, "hall")  # someone genuinely here now
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT
    assert ctrl._preset_reason[ZA] == "motion"
    assert ctrl._eco_low_wanted(ZA) is False


def test_an_ordinary_booking_leaves_no_tail():
    ctrl, hass = make_controller()
    hass.states.set("weather.forecast", "cloudy", {"temperature": 12.0})
    hall_temp(ctrl, 15.5)
    ctrl._record_booking_edges()
    booking(ctrl, ZA, "Beavers")
    ctrl._record_booking_edges()
    end_booking(ctrl, ZA)
    ctrl._record_booking_edges()
    motion(ctrl, "hall")
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT
    assert ctrl._preset_reason[ZA] == "motion"


def test_the_manual_override_still_asks_for_comfort():
    ctrl = _ended_eco_booking(12.0)
    ctrl._switches["zone_a_occupied_override"].is_on = True
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT
    assert ctrl._preset_reason[ZA] == "occupied_override"


def test_a_new_booking_starting_inside_the_tail_owns_the_zone():
    ctrl = _ended_eco_booking(12.0)
    booking(ctrl, ZA, "Beavers")
    ctrl._record_booking_edges()
    assert ctrl._eco_tail_active(ZA) is False
    motion(ctrl, "hall")
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT
    assert ctrl._preset_reason[ZA] == "booking"
