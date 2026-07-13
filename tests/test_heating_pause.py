"""The occupant "Pause hall heating" cutout.

The Rointe radiators are child-locked, so this is the only accessible way for
someone in the hall to stop the heat. It forces the hall to ice (still
frost-protected), above boost and bookings, hall-only; it clears on a
deliberate action (resume / hall boost) or when a fresh session emerges from an
idle gap, and it holds the winter destratification fans off so they cannot pull
roof-space heat down onto the person who is already too warm.
"""

from __future__ import annotations

from datetime import timedelta

from custom_components.scout_hut_heating.const import CONF_CEILING_TEMP, CONF_FAN_MASTER
from scout_testkit import (
    E,
    PRESET_COMFORT,
    PRESET_ICE,
    ZA,
    ZB,
    booking,
    boost,
    end_booking,
    make_controller,
    motion,
    run,
)

MASTER = "switch.fan_master"


def _events(ctrl, kind):
    return [e for e in ctrl.audit.to_list() if e["event"] == kind]


# --- Priority: pause forces hall ice over everything below it ------------------

def test_pause_forces_hall_ice_over_a_booking():
    ctrl, _ = make_controller()
    booking(ctrl, ZA)
    motion(ctrl, "hall")  # would otherwise be comfort
    ctrl.hall_heating_paused = True
    assert ctrl._desired_zone(ZA) == PRESET_ICE


def test_pause_forces_hall_ice_over_a_boost():
    ctrl, _ = make_controller()
    boost(ctrl, ZA)  # would otherwise be comfort
    ctrl.hall_heating_paused = True
    assert ctrl._desired_zone(ZA) == PRESET_ICE


def test_pause_is_hall_only_office_still_heats():
    ctrl, _ = make_controller()
    booking(ctrl, ZB)
    motion(ctrl, "office")
    ctrl.hall_heating_paused = True
    assert ctrl._desired_zone(ZB) == PRESET_COMFORT  # office untouched
    assert ctrl._desired_zone(ZA) == PRESET_ICE


# --- Mutual exclusion with the hall boost --------------------------------------

def test_pausing_clears_a_hall_boost():
    ctrl, _ = make_controller()
    boost(ctrl, ZA)
    run(ctrl.async_pause_hall_heating())
    assert ctrl.hall_heating_paused is True
    assert ctrl.boost_active(ZA) is False


def test_a_hall_boost_clears_the_pause():
    ctrl, _ = make_controller()
    run(ctrl.async_pause_hall_heating())
    run(ctrl.async_boost(ZA))
    assert ctrl.hall_heating_paused is False
    assert ctrl.boost_active(ZA) is True


def test_an_office_boost_does_not_clear_the_pause():
    ctrl, _ = make_controller()
    run(ctrl.async_pause_hall_heating())
    run(ctrl.async_boost(ZB))
    assert ctrl.hall_heating_paused is True


# --- Resume paths --------------------------------------------------------------

def test_resume_button_clears_the_pause():
    ctrl, _ = make_controller()
    run(ctrl.async_pause_hall_heating())
    run(ctrl.async_resume_hall_heating())
    assert ctrl.hall_heating_paused is False


def test_booking_end_clears_a_pause_carried_through_the_session():
    # Adjacent-booking case: paused mid-session, the running booking ends -> the
    # pause lifts so the next (adjacent) booking starts fresh in a warm room.
    ctrl, _ = make_controller()
    booking(ctrl, ZA)
    ctrl._record_booking_edges()  # baseline: booking running
    run(ctrl.async_pause_hall_heating())
    assert ctrl.hall_heating_paused is True
    end_booking(ctrl, ZA)
    ctrl._record_booking_edges()  # booking_end edge
    assert ctrl.hall_heating_paused is False


def test_office_booking_end_does_not_clear_a_hall_pause():
    ctrl, _ = make_controller()
    booking(ctrl, ZB)
    ctrl._record_booking_edges()
    run(ctrl.async_pause_hall_heating())
    end_booking(ctrl, ZB)
    ctrl._record_booking_edges()
    assert ctrl.hall_heating_paused is True


def test_a_fresh_session_from_idle_clears_the_pause(monkeypatch):
    from homeassistant.util import dt as dt_util

    ctrl, hass = make_controller()
    for eid in E["hall"]:
        hass.states.set(eid, "heat", {"current_temperature": 19.0})  # cold -> long lead
    ctrl._numbers["hall_comfort_temp"].native_value = 22
    ctrl._numbers["zone_a_warmup_rate"].native_value = 20  # 3 C x 20 = 60 min lead
    ctrl._numbers["zone_a_heatloss_pct"].native_value = 0
    run(ctrl.async_pause_hall_heating())

    start = dt_util.now() + timedelta(minutes=30)  # inside the 60 min lead

    async def _ev(cal, minutes):
        if cal == E["cal_hall"]:
            return [{"start": start.isoformat(), "summary": "Beavers"}]
        return []

    monkeypatch.setattr(ctrl, "_async_calendar_events", _ev)
    run(ctrl._async_refresh_calendars())

    assert ctrl.cal_window[ZA] is True  # pre-heat window opened from idle
    assert ctrl.hall_heating_paused is False


# --- Fans ----------------------------------------------------------------------

def test_pause_holds_the_winter_fans_off():
    ctrl, hass = make_controller(
        config_overrides={CONF_FAN_MASTER: MASTER, CONF_CEILING_TEMP: "sensor.ceiling"}
    )
    ctrl.seasonal_lockout = False  # winter regime
    for eid in E["hall"]:
        hass.states.set(eid, "heat", {"current_temperature": 20.0})  # floor 20 < cap
    hass.states.set("sensor.ceiling", "23.0")  # dt 3 > dt_on
    motion(ctrl, "hall")  # occupied -> the recirc gate is satisfied

    assert ctrl._fan_target()[0] is True  # would run without the pause
    ctrl.hall_heating_paused = True
    assert ctrl._fan_target() == (False, None, "off")


def test_pause_leaves_the_summer_breeze_alone():
    ctrl, hass = make_controller(
        config_overrides={CONF_FAN_MASTER: MASTER, CONF_CEILING_TEMP: "sensor.ceiling"}
    )
    ctrl.seasonal_lockout = True  # summer cooling regime
    for eid in E["hall"]:
        hass.states.set(eid, "heat", {"current_temperature": 26.0})  # warm
    hass.states.set("sensor.ceiling", "27.0")
    motion(ctrl, "hall")
    ctrl.hall_heating_paused = True
    # A heating pause must not kill the cooling breeze a hot person wants.
    assert ctrl._fan_target() == (True, "forward", "summer")


# --- Audit + persistence -------------------------------------------------------

def test_pause_and_resume_are_audited():
    ctrl, _ = make_controller()
    run(ctrl.async_pause_hall_heating())
    run(ctrl.async_resume_hall_heating())
    assert _events(ctrl, "heating_paused")
    resumed = _events(ctrl, "heating_resumed")
    assert resumed and resumed[-1]["reason"] == "manual"


def test_pause_is_persisted_in_the_snapshot():
    ctrl, _ = make_controller()
    ctrl.hall_heating_paused = True
    assert ctrl._state_snapshot()["hall_heating_paused"] is True
