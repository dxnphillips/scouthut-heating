"""The eco tail an ECO-keyword booking leaves behind (v1.49.0).

Field 2026-10-02: two sal-vation fitness-class bookings (an ECO booking) ended
at 06:00Z and 10:15Z with someone still moving about the hall, and on each end
edge the bare-occupancy rung lit all four hall heaters toward comfort 19 on a
15–17 °C hall the booking had just said should sit at eco — 6–10 minutes of
firing each time until the alarm iced them, plus panels at 35 °C warming an
empty hall afterwards. The person still tripping the PIR after a low-key booking
is that booking's visit, so after an eco booking ends the zone heats toward eco,
not comfort, until the building has been quiet for a motion timeout.

Since v1.51.0 eco is one temperature (16) in every zone: the classes found 16
fine, so the separate eco-low slider (14) went.
"""

from scout_testkit import (
    E,
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
    shared_temp,
)


def _ended_eco_booking(temp):
    ctrl, hass = make_controller()
    hass.states.set("weather.forecast", "cloudy", {"temperature": 12.0})
    ctrl._numbers["hall_comfort_temp"].native_value = 19.0
    hall_temp(ctrl, temp)
    ctrl._record_booking_edges()  # baseline: calendar off
    booking(ctrl, ZA, "sal-vation fitness")
    ctrl._record_booking_edges()  # start edge: eco booking latched
    end_booking(ctrl, ZA)
    ctrl._record_booking_edges()  # end edge: the tail begins
    ctrl.cal_title[ZA] = ""  # the look-ahead refresh the end edge flags clears it
    return ctrl


def test_the_class_finishing_up_does_not_light_comfort():
    # A hall above eco 16 at the end of an ECO booking: no heat at all.
    ctrl = _ended_eco_booking(16.5)
    motion(ctrl, "hall")
    assert ctrl._desired_zone(ZA) == PRESET_ICE
    assert ctrl._preset_reason[ZA] == "occupied_warm"


def test_a_cold_hall_in_the_tail_heats_toward_eco_not_comfort():
    # 2026-10-02 06:00Z: hall 15.5 under eco 16 -> eco, not comfort 19.
    ctrl = _ended_eco_booking(15.5)
    motion(ctrl, "hall")
    assert ctrl._desired_zone(ZA) == PRESET_ECO
    assert ctrl._preset_reason[ZA] == "eco_tail"


def test_the_tail_lasts_one_motion_timeout_then_occupancy_is_ordinary_again():
    ctrl = _ended_eco_booking(16.5)
    motion(ctrl, "hall")
    assert ctrl._desired_zone(ZA) == PRESET_ICE
    advance(ctrl, ctrl.number("motion_timeout_minutes") + 1)
    motion(ctrl, "hall")  # someone genuinely here now
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT
    assert ctrl._preset_reason[ZA] == "motion"
    assert ctrl._eco_tail_active(ZA) is False


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


# --- The shared block follows the tail (v1.50.7) -----------------------------
def test_someone_in_the_kitchen_after_the_booking_keeps_it_at_eco():
    # 2026-10-23 10:15:01Z: the second sal-vation booking ended and the shared
    # went eco -> comfort on a kitchen trip 13 minutes old.
    ctrl = _ended_eco_booking(15.5)
    shared_temp(ctrl, 15.0)  # under eco: heats, but toward eco, not comfort
    motion(ctrl, "kitchen")
    assert ctrl._desired_zone("kitchen") == PRESET_ECO
    assert ctrl._preset_reason["kitchen"] == "eco_tail"


def test_a_kitchen_already_above_eco_in_the_tail_rests_on_ice():
    # One ladder (v1.51.0): a warm occupied room lands on ice, as the hall does.
    ctrl = _ended_eco_booking(15.5)
    shared_temp(ctrl, 18.0)
    motion(ctrl, "kitchen")
    assert ctrl._desired_zone("kitchen") == PRESET_ICE
    assert ctrl._preset_reason["kitchen"] == "occupied_warm"


def test_after_the_tail_a_kitchen_trip_heats_the_shared_as_before():
    ctrl = _ended_eco_booking(15.5)
    shared_temp(ctrl, 18.0)
    advance(ctrl, ctrl.number("motion_timeout_minutes") + 1)
    motion(ctrl, "kitchen")
    assert ctrl._desired_zone("kitchen") == PRESET_COMFORT
    assert ctrl._preset_reason["kitchen"] == "motion"


# --- The tail lasts the visit (v1.51.0) ----------------------------------------
# Field 2026-10-30: the 09:45-11:15Z sal-vation booking overran by an hour with the
# hall PIR never quiet. The 15-min tail ran out at 11:30Z and the hall and the
# shared block went to comfort 19 until the alarm at 12:18Z.
def test_a_class_that_overruns_stays_on_eco():
    ctrl = _ended_eco_booking(12.0)
    for _ in range(6):  # an hour, never quiet for a whole motion timeout
        motion(ctrl, "hall")
        assert ctrl._desired_zone(ZA) == PRESET_ECO
        assert ctrl._preset_reason[ZA] == "eco_tail"
        advance(ctrl, 10)
    motion(ctrl, "hall")
    assert ctrl._desired_zone(ZA) == PRESET_ECO
    assert ctrl._eco_tail_active(ZA) is True


def test_motion_anywhere_in_the_building_carries_the_tail():
    ctrl = _ended_eco_booking(12.0)
    motion(ctrl, "hall")
    for _ in range(4):
        advance(ctrl, 10)
        motion(ctrl, "female")  # in the toilets
        assert ctrl._eco_tail_active(ZA) is True
    motion(ctrl, "hall")
    assert ctrl._desired_zone(ZA) == PRESET_ECO


def test_an_away_arm_ends_the_visit():
    # 12:18Z armed and gone; someone else arrived at 12:28Z, inside the timeout.
    ctrl = _ended_eco_booking(12.0)
    motion(ctrl, "hall")
    advance(ctrl, 10)
    ctrl.hass.states.set(E["alarm_main"], "armed_away")
    assert ctrl._eco_tail_active(ZA) is False
    ctrl.hass.states.set(E["alarm_main"], "disarmed")
    advance(ctrl, 10)
    motion(ctrl, "hall")
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT
    assert ctrl._preset_reason[ZA] == "motion"


def test_a_quiet_spell_is_not_bridged_by_a_later_arrival():
    ctrl = _ended_eco_booking(12.0)
    motion(ctrl, "hall")
    advance(ctrl, ctrl.number("motion_timeout_minutes") + 5)
    motion(ctrl, "hall")  # after the building had gone quiet
    assert ctrl._eco_tail_active(ZA) is False
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT


def test_the_kitchen_stays_at_eco_while_the_class_is_still_there():
    ctrl = _ended_eco_booking(15.5)
    shared_temp(ctrl, 15.0)
    for _ in range(5):
        motion(ctrl, "kitchen")
        assert ctrl._desired_zone("kitchen") == PRESET_ECO
        assert ctrl._preset_reason["kitchen"] == "eco_tail"
        advance(ctrl, 10)
