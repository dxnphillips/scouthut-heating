"""A running booking's empty hall: a group on its way, or a group gone (v1.50.8).

Field 2026-10-23, the Friday 16:30Z booking. The pre-heat left the hall 0.25
short at the start, nobody had arrived, and `booking_quiet` dropped it to eco
until the group walked in at 16:39Z. The group then armed the alarm and left at
19:09Z, the calendar event ran on past midnight, and the hall held eco 16 in an
empty, alarmed building with all four radiators firing from 22:21Z, because a
running booking overrode the alarm entirely.

For `BOOKING_ARRIVAL_GRACE_MIN` after the start an empty zone keeps the
booking's heat; after it, an away-armed alarm with nobody moving ices the zone.
"""

from datetime import timedelta

import custom_components.scout_hut_heating.coordinator as C
from scout_testkit import (
    E,
    PRESET_COMFORT,
    PRESET_ECO,
    PRESET_ICE,
    ZA,
    ZB,
    advance,
    booking,
    end_booking,
    hall_temp,
    make_controller,
    motion,
    preheat_window,
    run,
    zone_temp,
)


def _started(ctrl, zone, minutes_ago, title=""):
    """A running booking whose calendar entity says it began ``minutes_ago``."""
    booking(ctrl, zone, title)
    start = ctrl._now() - timedelta(minutes=minutes_ago)
    cal = ctrl.config.get(C.ZONE_CALENDAR[zone])
    ctrl.hass.states.set(
        cal, "on", {"message": title, "start_time": start.strftime("%Y-%m-%d %H:%M:%S")}
    )


def _friday():
    ctrl, hass = make_controller()
    ctrl._numbers["hall_comfort_temp"].native_value = 19.0
    ctrl._numbers["booking_hold_cap"].native_value = 0.0
    return ctrl, hass


def _armed(hass):
    hass.states.set(E["alarm_main"], "armed_away")


# --- The group has been and gone -------------------------------------------------
def test_an_armed_empty_booking_past_its_grace_is_iced():
    ctrl, hass = _friday()
    _started(ctrl, ZA, 300)  # 19:09Z -> past midnight
    hall_temp(ctrl, 15.5)  # cold enough that eco 16 had all four firing
    _armed(hass)
    assert ctrl._desired_zone(ZA) == PRESET_ICE
    assert ctrl._preset_reason[ZA] == "booking_armed"


def test_without_the_alarm_an_empty_booking_still_rests_at_eco():
    ctrl, _ = _friday()
    _started(ctrl, ZA, 300)
    hall_temp(ctrl, 15.5)
    assert ctrl._desired_zone(ZA) == PRESET_ECO
    assert ctrl._preset_reason[ZA] == "booking_quiet"


def test_the_last_one_out_keeps_heat_for_the_motion_timeout():
    ctrl, hass = _friday()
    _started(ctrl, ZA, 160)
    hall_temp(ctrl, 18.5)
    motion(ctrl, "hall")
    _armed(hass)
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT
    advance(ctrl, ctrl.number("motion_timeout_minutes") + 1)
    assert ctrl._desired_zone(ZA) == PRESET_ICE


def test_an_eco_keyword_booking_is_iced_too():
    ctrl, hass = _friday()
    _started(ctrl, ZA, 60, "Sal-vation clean")
    hall_temp(ctrl, 12.0)  # below the eco-low 14
    _armed(hass)
    assert ctrl._desired_zone(ZA) == PRESET_ICE
    assert ctrl._preset_reason[ZA] == "booking_armed"


def test_the_office_follows_its_own_panel():
    ctrl, hass = _friday()
    _started(ctrl, ZB, 60)
    zone_temp(ctrl, ZB, 15.0)
    hass.states.set(E["alarm_office"], "armed_away")
    assert ctrl._desired_zone(ZB) == PRESET_ICE
    assert ctrl._preset_reason[ZB] == "booking_armed"


def test_a_night_arm_is_people_asleep_not_people_gone():
    ctrl, hass = _friday()
    _started(ctrl, ZA, 300)
    hall_temp(ctrl, 17.0)
    hass.states.set(E["alarm_main"], "armed_night")
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT


def test_the_occupied_override_still_holds_the_heat():
    ctrl, hass = _friday()
    _started(ctrl, ZA, 300)
    hall_temp(ctrl, 17.0)
    _armed(hass)
    ctrl._switches["zone_a_occupied_override"].is_on = True
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT


def test_coming_back_and_disarming_heats_again():
    ctrl, hass = _friday()
    _started(ctrl, ZA, 300)
    hall_temp(ctrl, 15.5)
    _armed(hass)
    assert ctrl._desired_zone(ZA) == PRESET_ICE
    hass.states.set(E["alarm_main"], "disarmed")
    motion(ctrl, "hall")
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT


# --- The group is on its way ------------------------------------------------------
def test_a_late_group_finds_the_pre_heat_still_running():
    # 16:30Z: 0.25 short, nobody in, the alarm still set from before.
    ctrl, hass = _friday()
    _started(ctrl, ZA, 1)
    hall_temp(ctrl, 18.75)
    _armed(hass)
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT
    assert ctrl._preset_reason[ZA] == "booking"


def test_the_empty_start_rests_at_eco_only_after_the_grace():
    ctrl, _ = _friday()
    _started(ctrl, ZA, C.BOOKING_ARRIVAL_GRACE_MIN - 1)
    hall_temp(ctrl, 18.75)
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT
    _started(ctrl, ZA, C.BOOKING_ARRIVAL_GRACE_MIN + 1)
    assert ctrl._desired_zone(ZA) == PRESET_ECO
    assert ctrl._preset_reason[ZA] == "booking_quiet"


def test_a_warm_room_at_the_start_still_rests_on_ice():
    ctrl, _ = _friday()
    _started(ctrl, ZA, 1)
    hall_temp(ctrl, 19.5)
    assert ctrl._desired_zone(ZA) == PRESET_ICE
    assert ctrl._preset_reason[ZA] == "booking_warm"


def test_the_pre_heat_window_ignores_the_alarm():
    ctrl, hass = _friday()
    preheat_window(ctrl, ZA)
    hall_temp(ctrl, 16.0)
    _armed(hass)
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT
    assert ctrl._preset_reason[ZA] == "preheat"


# --- Knowing when it started ----------------------------------------------------
def test_an_unknown_start_behaves_as_before():
    # No start_time on the entity and no start edge seen: no grace, no ice.
    ctrl, hass = _friday()
    booking(ctrl, ZA)
    hall_temp(ctrl, 15.5)
    _armed(hass)
    assert ctrl._booking_running_minutes(ZA) is None
    assert ctrl._desired_zone(ZA) == PRESET_ECO
    assert ctrl._preset_reason[ZA] == "booking_quiet"


def test_the_start_edge_this_controller_saw_is_the_fallback():
    ctrl, hass = _friday()
    hall_temp(ctrl, 15.5)
    run(ctrl.async_reconcile())  # calendar seen off
    booking(ctrl, ZA)
    run(ctrl.async_reconcile())  # start edge
    assert ctrl._booking_running_minutes(ZA) is not None
    assert ctrl.applied[ZA] == PRESET_COMFORT  # empty, but inside the grace
    _armed(hass)
    advance(ctrl, C.BOOKING_ARRIVAL_GRACE_MIN + 1)
    run(ctrl.async_reconcile())
    assert ctrl.applied[ZA] == PRESET_ICE
    assert ctrl._preset_reason[ZA] == "booking_armed"
    end_booking(ctrl, ZA)
    run(ctrl.async_reconcile())
    assert ctrl._booking_started_at[ZA] is None


def test_a_restart_mid_booking_reads_the_start_from_the_calendar():
    # The fix lands with the weekend booking still running: no edge is seen,
    # the entity's own start_time carries it.
    ctrl, hass = _friday()
    _started(ctrl, ZA, 600)
    hall_temp(ctrl, 15.5)
    _armed(hass)
    run(ctrl.async_reconcile())
    assert ctrl.applied[ZA] == PRESET_ICE
    running = ctrl.diagnostics_data()["state"]["booking_running_min"]
    assert running[ZA] == 600.0 and running[ZB] is None
