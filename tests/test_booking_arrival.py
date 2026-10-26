"""A running booking's empty hall: a group on its way, or one coming back (v1.50.8).

Field 2026-10-23, the Friday 16:30Z booking. The pre-heat left the hall 0.25
short at the start, nobody had arrived, and `booking_quiet` dropped it to eco
until the group walked in at 16:39Z. For `BOOKING_ARRIVAL_GRACE_MIN` after the
start an empty zone now keeps the booking's heat.

The same evening the group armed the alarm and left at 19:09Z and a 24-hour
booking took over at 19:30Z; the hall rested at eco 16 through it. That is
wanted: if it is booked the group is expected back (owner, 2026-10-24), so an
away-armed alarm does not end a running booking.
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


# --- Booked means they are coming back ------------------------------------------
def test_an_armed_empty_booking_rests_at_eco_for_the_return():
    ctrl, hass = _friday()
    _started(ctrl, ZA, 300)
    hall_temp(ctrl, 15.5)
    _armed(hass)
    assert ctrl._desired_zone(ZA) == PRESET_ECO
    assert ctrl._preset_reason[ZA] == "booking_quiet"


def test_the_office_booking_does_too():
    ctrl, hass = _friday()
    _started(ctrl, ZB, 300)
    zone_temp(ctrl, ZB, 15.0)
    hass.states.set(E["alarm_office"], "armed_away")
    assert ctrl._desired_zone(ZB) == PRESET_ECO


def test_a_night_arm_is_people_asleep():
    ctrl, hass = _friday()
    _started(ctrl, ZA, 300)
    hall_temp(ctrl, 17.0)
    hass.states.set(E["alarm_main"], "armed_night")
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT


def test_the_returning_group_gets_comfort():
    ctrl, hass = _friday()
    _started(ctrl, ZA, 300)
    hall_temp(ctrl, 16.0)
    _armed(hass)
    assert ctrl._desired_zone(ZA) == PRESET_ECO
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
    # No start_time on the entity and no start edge seen: no grace.
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
    advance(ctrl, C.BOOKING_ARRIVAL_GRACE_MIN + 1)
    run(ctrl.async_reconcile())
    assert ctrl.applied[ZA] == PRESET_ECO
    assert ctrl._preset_reason[ZA] == "booking_quiet"
    end_booking(ctrl, ZA)
    run(ctrl.async_reconcile())
    assert ctrl._booking_started_at[ZA] is None


def test_a_restart_mid_booking_reads_the_start_from_the_calendar():
    # No edge is seen after a restart; the entity's own start_time carries it.
    ctrl, hass = _friday()
    _started(ctrl, ZA, 10)
    hall_temp(ctrl, 18.5)
    run(ctrl.async_reconcile())
    assert ctrl.applied[ZA] == PRESET_COMFORT  # still inside the arrival grace
    _started(ctrl, ZA, 600)
    run(ctrl.async_reconcile())
    assert ctrl.applied[ZA] == PRESET_ECO
    running = ctrl.diagnostics_data()["state"]["booking_running_min"]
    assert running[ZA] == 600.0 and running[ZB] is None


# --- Back-to-back bookings (a handover) -------------------------------------------
# Field 2026-10-23: a 24-hour booking began at 19:30Z the minute the Friday one
# ended. The calendar entity never went off, so neither edge was seen.
def _events(ctrl, name):
    return [e for e in ctrl.audit.to_list() if e.get("event") == name]


def _handover(ctrl, title="24 hour booking"):
    """The running event changes to one that starts now; the entity stays on."""
    cal = ctrl.config.get(C.ZONE_CALENDAR[ZA])
    ctrl.hass.states.set(
        cal,
        "on",
        {"message": title, "start_time": ctrl._now().strftime("%Y-%m-%d %H:%M:%S")},
    )


def _friday_running(hass_title=""):
    ctrl, hass = _friday()
    hall_temp(ctrl, 18.5)
    run(ctrl.async_reconcile())  # calendar seen off
    # Started three hours back by the calendar's own clock (the test clock
    # does not move; `advance` ages stored stamps instead).
    _started(ctrl, ZA, 180, hass_title)
    run(ctrl.async_reconcile())  # the Friday booking starts
    return ctrl, hass


def test_a_handover_is_an_end_and_a_start():
    ctrl, hass = _friday_running()
    advance(ctrl, 180)
    _handover(ctrl)
    run(ctrl.async_reconcile())
    (end,) = _events(ctrl, "booking_end")
    assert end["handover"] is True
    starts = _events(ctrl, "booking_start")
    assert len(starts) == 2 and starts[-1]["handover"] is True
    assert ctrl._booking_running_minutes(ZA) < 1


def test_a_handover_gets_no_arrival_grace():
    # 19:09Z armed and gone, 19:30Z the 24-hour booking takes over: no arrival
    # grace (there was no pre-heat to finish), so the empty hall stays at eco
    # rather than relighting comfort for half an hour.
    ctrl, hass = _friday_running()
    advance(ctrl, 180)
    _armed(hass)
    _handover(ctrl)
    run(ctrl.async_reconcile())
    assert ctrl.applied[ZA] == PRESET_ECO
    assert ctrl._preset_reason[ZA] == "booking_quiet"


def test_a_handover_lifts_the_hall_pause():
    ctrl, hass = _friday_running()
    ctrl.hall_heating_paused = True
    advance(ctrl, 180)
    _handover(ctrl)
    run(ctrl.async_reconcile())
    assert ctrl.hall_heating_paused is False
    (resumed,) = _events(ctrl, "heating_resumed")
    assert resumed["reason"] == "booking_end"


def test_the_eco_latch_follows_the_new_event():
    ctrl, hass = _friday_running("Sal-vation clean")
    assert _events(ctrl, "booking_start")[-1]["eco"] is True
    advance(ctrl, 60)
    _handover(ctrl, "Cubs")
    run(ctrl.async_reconcile())
    assert _events(ctrl, "booking_start")[-1]["eco"] is False
    assert ctrl._cal_eco_running[ZA] is False


def test_an_event_edited_in_place_is_not_a_handover():
    ctrl, hass = _friday_running()
    cal = ctrl.config.get(C.ZONE_CALENDAR[ZA])
    start = hass.states.get(cal).attributes["start_time"]
    hass.states.set(cal, "on", {"message": "renamed", "start_time": start})
    run(ctrl.async_reconcile())
    assert not _events(ctrl, "booking_end")
    assert len(_events(ctrl, "booking_start")) == 1
