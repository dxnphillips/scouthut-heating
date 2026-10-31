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


# --- The shared block follows the tail (v1.50.7) -----------------------------
def test_the_cleaner_in_the_kitchen_after_the_booking_keeps_the_shared_at_eco():
    # 2026-10-23 10:15:01Z: the second sal-vation booking ended and the shared
    # went eco -> comfort on a kitchen trip 13 minutes old.
    ctrl = _ended_eco_booking(15.5)
    shared_temp(ctrl, 18.0)  # under shared comfort: an ordinary trip would heat
    motion(ctrl, "kitchen")
    assert ctrl._desired_room("kitchen") == PRESET_ECO
    assert ctrl._preset_reason["kitchen"] == "eco_tail"


def test_after_the_tail_a_kitchen_trip_heats_the_shared_as_before():
    ctrl = _ended_eco_booking(15.5)
    shared_temp(ctrl, 18.0)
    advance(ctrl, ctrl.number("motion_timeout_minutes") + 1)
    motion(ctrl, "kitchen")
    assert ctrl._desired_room("kitchen") == PRESET_COMFORT
    assert ctrl._preset_reason["kitchen"] == "motion"


# --- The tail lasts the visit (v1.51.0) ----------------------------------------
# Field 2026-10-30: the 09:45-11:15Z sal-vation clean overran by an hour with the
# hall PIR never quiet. The 15-min tail ran out at 11:30Z and the hall and the
# shared block went to comfort 19 for the cleaner until the alarm at 12:18Z.
def test_a_cleaner_who_overruns_stays_on_the_eco_low_target():
    ctrl = _ended_eco_booking(12.0)
    for _ in range(6):  # an hour, never quiet for a whole motion timeout
        motion(ctrl, "hall")
        assert ctrl._desired_zone(ZA) == PRESET_ECO
        assert ctrl._preset_reason[ZA] == "eco_tail"
        advance(ctrl, 10)
    motion(ctrl, "hall")
    assert ctrl._desired_zone(ZA) == PRESET_ECO
    assert ctrl._eco_low_wanted(ZA) is True


def test_motion_anywhere_in_the_building_carries_the_tail():
    ctrl = _ended_eco_booking(12.0)
    motion(ctrl, "hall")
    for _ in range(4):
        advance(ctrl, 10)
        motion(ctrl, "female")  # cleaning the toilets
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


def test_the_shared_block_stays_at_eco_while_the_cleaner_is_still_there():
    ctrl = _ended_eco_booking(15.5)
    shared_temp(ctrl, 18.0)
    for _ in range(5):
        motion(ctrl, "kitchen")
        assert ctrl._desired_room("kitchen") == PRESET_ECO
        assert ctrl._preset_reason["kitchen"] == "eco_tail"
        advance(ctrl, 10)
