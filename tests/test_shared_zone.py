"""The kitchen, gents and ladies: three zones run the way the hall is (v1.51.0).

They used to be one block that went to frost protection whenever both alarm
panels were set, so it started heating only when someone disarmed on arrival.
In the week of 2026-10-26 the coldest of the three was 13-16 at every evening
booking's start, took 70-95 min to reach 19, and on two of three evenings never
got there before the group left.
"""

from datetime import timedelta

import pytest

from custom_components.scout_hut_heating.const import CONF_SHARED_CLIMATES
from homeassistant.util import dt as dt_util
from scout_testkit import (
    E,
    PRESET_COMFORT,
    PRESET_ECO,
    PRESET_ICE,
    ZA,
    ZB,
    advance,
    booking,
    boost,
    make_controller,
    motion,
    on,
    preheat_window,
    run,
    service_calls,
    shared_temp,
)

ROOMS = ("kitchen", "gents", "ladies")
KITCHEN, GENTS, LADIES = E["shared"]  # climate.kitchen, climate.gents, climate.ladies


def _room_temps(hass, kitchen, gents, ladies):
    for climate, temp in ((KITCHEN, kitchen), (GENTS, gents), (LADIES, ladies)):
        hass.states.set(climate, "heat", {"current_temperature": temp})


def _desired(ctrl):
    return {room: ctrl._desired_zone(room) for room in ROOMS}


def _armed(hass):
    hass.states.set(E["alarm_main"], "armed_away")
    hass.states.set(E["alarm_office"], "armed_away")


def _events(ctrl, name):
    return [e for e in ctrl.audit.to_list() if e.get("event") == name]


# --- Which heater is which room --------------------------------------------------
def test_each_shared_heater_is_its_own_room():
    ctrl, _ = make_controller()
    assert ctrl._room_climates() == {
        "kitchen": [KITCHEN],
        "gents": [GENTS],
        "ladies": [LADIES],
    }


def test_rooms_are_matched_by_name_not_order():
    ctrl, _ = make_controller(
        {CONF_SHARED_CLIMATES: ["climate.ladies_toilet", "climate.kitchen", "climate.gents_toilet"]}
    )
    assert ctrl._room_climates() == {
        "kitchen": ["climate.kitchen"],
        "gents": ["climate.gents_toilet"],
        "ladies": ["climate.ladies_toilet"],
    }


def test_a_heater_with_an_unknown_name_still_gets_a_room():
    ctrl, _ = make_controller({CONF_SHARED_CLIMATES: ["climate.kitchen", "climate.store"]})
    assert ctrl._room_climates() == {"kitchen": ["climate.kitchen"], "gents": ["climate.store"]}


def test_no_shared_heaters_means_no_rooms():
    ctrl, _ = make_controller({CONF_SHARED_CLIMATES: []})
    assert ctrl._rooms() == []
    assert ctrl._desired_zone("kitchen") is None


# --- Nobody, somebody ------------------------------------------------------------
def test_an_empty_building_is_ice():
    ctrl, _ = make_controller()
    assert set(_desired(ctrl).values()) == {PRESET_ICE}
    assert ctrl._preset_reason["gents"] == "building_empty"


def test_each_room_heats_on_its_own_pir():
    ctrl, hass = make_controller()
    ctrl._numbers["shared_comfort_temp"].native_value = 19.0
    _room_temps(hass, 15.0, 15.0, 15.0)
    motion(ctrl, "gents")
    assert _desired(ctrl) == {"kitchen": PRESET_ECO, "gents": PRESET_COMFORT, "ladies": PRESET_ECO}
    assert ctrl._preset_reason["gents"] == "motion"
    assert ctrl._preset_reason["kitchen"] == "others_present"


def test_the_ladies_pir_is_the_female_sensor():
    ctrl, hass = make_controller()
    _room_temps(hass, 15.0, 15.0, 15.0)
    motion(ctrl, "female")
    assert ctrl._desired_zone("ladies") == PRESET_COMFORT


def test_a_warm_occupied_room_rests_on_ice():
    ctrl, hass = make_controller()
    ctrl._numbers["shared_comfort_temp"].native_value = 19.0
    _room_temps(hass, 20.0, 15.0, 15.0)
    motion(ctrl, "kitchen")
    assert ctrl._desired_zone("kitchen") == PRESET_ICE
    assert ctrl._preset_reason["kitchen"] == "occupied_warm"


def test_hall_motion_rests_the_rooms_at_eco():
    ctrl, hass = make_controller()
    _room_temps(hass, 15.0, 15.0, 15.0)
    motion(ctrl, "hall")
    assert set(_desired(ctrl).values()) == {PRESET_ECO}


# --- Bookings ----------------------------------------------------------------------
def test_a_booking_with_the_group_in_heats_every_cold_room():
    ctrl, hass = make_controller()
    ctrl._numbers["shared_comfort_temp"].native_value = 19.0
    _room_temps(hass, 15.0, 15.0, 15.0)
    booking(ctrl, ZA)
    motion(ctrl, "hall")
    assert set(_desired(ctrl).values()) == {PRESET_COMFORT}
    assert ctrl._preset_reason["ladies"] == "booking"


def test_an_office_booking_counts_too():
    ctrl, hass = make_controller()
    _room_temps(hass, 15.0, 15.0, 15.0)
    booking(ctrl, ZB)
    motion(ctrl, "office")
    assert set(_desired(ctrl).values()) == {PRESET_COMFORT}


def test_each_room_is_judged_on_its_own_reading():
    # The kitchen is warm, the toilets are not: only the toilets heat.
    ctrl, hass = make_controller()
    ctrl._numbers["shared_comfort_temp"].native_value = 19.0
    _room_temps(hass, 19.5, 16.0, 17.0)
    booking(ctrl, ZA)
    motion(ctrl, "hall")
    assert _desired(ctrl) == {"kitchen": PRESET_ICE, "gents": PRESET_COMFORT, "ladies": PRESET_COMFORT}
    assert ctrl._preset_reason["kitchen"] == "booking_warm"


def test_a_heating_room_holds_comfort_through_the_release_band():
    ctrl, hass = make_controller()
    ctrl._numbers["shared_comfort_temp"].native_value = 19.0
    booking(ctrl, ZA)
    motion(ctrl, "hall")
    _room_temps(hass, 19.25, 19.25, 19.25)
    ctrl.applied["gents"] = PRESET_COMFORT
    assert ctrl._desired_zone("gents") == PRESET_COMFORT  # still under 19.5
    assert ctrl._desired_zone("kitchen") == PRESET_ICE  # not heating: judged bare


def test_a_booking_holds_the_rooms_whatever_the_alarm_says():
    # The fix: the hall already ignored the alarm for a booking; the rooms
    # iced until someone disarmed.
    ctrl, hass = make_controller()
    _room_temps(hass, 14.0, 14.0, 14.0)
    _armed(hass)
    booking(ctrl, ZA)
    motion(ctrl, "hall")
    assert set(_desired(ctrl).values()) == {PRESET_COMFORT}


def test_an_empty_building_mid_booking_rests_the_rooms_at_eco():
    ctrl, hass = make_controller()
    _room_temps(hass, 15.0, 15.0, 15.0)
    booking(ctrl, ZA)  # no start known, nobody anywhere
    assert set(_desired(ctrl).values()) == {PRESET_ECO}
    assert ctrl._preset_reason["kitchen"] == "booking_quiet"


def test_a_group_in_any_room_keeps_every_room_at_comfort():
    # A toilet is empty most of a session; the group being in the hut is what
    # holds it, not the toilet's own PIR.
    ctrl, hass = make_controller()
    _room_temps(hass, 15.0, 15.0, 15.0)
    booking(ctrl, ZA)
    motion(ctrl, "kitchen")
    assert ctrl._desired_zone("gents") == PRESET_COMFORT


def test_the_arrival_grace_keeps_the_rooms_heating():
    ctrl, hass = make_controller()
    _room_temps(hass, 15.0, 15.0, 15.0)
    booking(ctrl, ZA)
    cal = E["cal_hall"]
    start = ctrl._now() - timedelta(minutes=10)
    hass.states.set(cal, "on", {"message": "", "start_time": start.strftime("%Y-%m-%d %H:%M:%S")})
    assert ctrl._desired_zone("kitchen") == PRESET_COMFORT
    start = ctrl._now() - timedelta(minutes=40)
    hass.states.set(cal, "on", {"message": "", "start_time": start.strftime("%Y-%m-%d %H:%M:%S")})
    assert ctrl._desired_zone("kitchen") == PRESET_ECO


def test_an_eco_keyword_booking_rests_them_at_eco():
    ctrl, hass = make_controller()
    _room_temps(hass, 13.0, 13.0, 13.0)
    booking(ctrl, ZA, "sal-vation fitness")
    motion(ctrl, "kitchen")
    assert set(_desired(ctrl).values()) == {PRESET_ECO}
    assert ctrl._preset_reason["kitchen"] == "booking_eco"


def test_a_real_booking_beside_an_eco_one_wins():
    ctrl, hass = make_controller()
    _room_temps(hass, 15.0, 15.0, 15.0)
    booking(ctrl, ZA, "sal-vation")
    booking(ctrl, ZB)
    motion(ctrl, "office")
    assert set(_desired(ctrl).values()) == {PRESET_COMFORT}


# --- Pre-heat ------------------------------------------------------------------------
def _upcoming(ctrl, monkeypatch, minutes, title="Beavers"):
    start = dt_util.now() + timedelta(minutes=minutes)

    async def _calendar(cal, _minutes):
        if cal == E["cal_hall"]:
            return [{"start": start.isoformat(), "summary": title}]
        return []

    monkeypatch.setattr(ctrl, "_async_calendar_events", _calendar)
    run(ctrl._async_refresh_calendars())
    return start


def _cold_evening(hass):
    hass.states.set(E["weather"], "cloudy", {"temperature": 11.0})


def test_each_room_opens_its_pre_heat_on_its_own_lead(monkeypatch):
    # 90 min out at outdoor 11: the gents at 13 needs ~180 min, the ladies at 16
    # ~125, the kitchen at 18.5 ~75 — only the two cold rooms start.
    ctrl, hass = make_controller()
    ctrl._numbers["shared_comfort_temp"].native_value = 19.0
    ctrl._numbers["preheat_minutes"].native_value = 240
    _cold_evening(hass)
    _room_temps(hass, 18.5, 13.0, 16.0)
    start = _upcoming(ctrl, monkeypatch, 90)
    assert ctrl.cal_window["gents"] is True
    assert ctrl.cal_window["ladies"] is True
    assert ctrl.cal_window["kitchen"] is False
    assert ctrl._preheat_open_for["gents"] == start
    started = {e["zone"]: e for e in _events(ctrl, "preheat_start")}
    assert started["gents"]["lead_min"] > started["ladies"]["lead_min"] > 90
    assert started["gents"]["rate_key"] == "gents_warmup_rate"
    assert "kitchen" not in started


def test_the_pre_heat_runs_with_the_alarm_set(monkeypatch):
    ctrl, hass = make_controller()
    ctrl._numbers["shared_comfort_temp"].native_value = 19.0
    _cold_evening(hass)
    _room_temps(hass, 18.5, 13.0, 16.0)
    _armed(hass)
    _upcoming(ctrl, monkeypatch, 90)
    assert ctrl._desired_zone("gents") == PRESET_COMFORT
    assert ctrl._preset_reason["gents"] == "preheat"
    assert ctrl._desired_zone("kitchen") == PRESET_ICE  # its window is not open yet
    assert ctrl._preset_reason["kitchen"] == "alarm"


def test_an_eco_keyword_booking_pre_heats_them_to_eco(monkeypatch):
    # One ladder for every zone: an ECO booking pre-heats the rooms toward eco,
    # as it does the hall, on each room's own lead to the eco target.
    ctrl, hass = make_controller()
    _cold_evening(hass)
    _room_temps(hass, 13.0, 13.0, 17.0)
    _upcoming(ctrl, monkeypatch, 30, "sal-vation fitness")
    assert ctrl.cal_window["gents"] is True and ctrl._room_window_eco["gents"] is True
    assert ctrl._desired_zone("gents") == PRESET_ECO
    assert ctrl._preset_reason["gents"] == "booking_eco"  # as the hall's eco pre-heat
    started = {e["zone"]: e for e in _events(ctrl, "preheat_start")}
    assert started["gents"]["target"] == ctrl.number("hall_eco_temp")
    # The ladies at 17 are already above eco: no window.
    assert ctrl.cal_window["ladies"] is False


def test_a_comfort_booking_beats_an_eco_one_starting_together(monkeypatch):
    ctrl, hass = make_controller()
    _cold_evening(hass)
    _room_temps(hass, 13.0, 13.0, 13.0)
    start = dt_util.now() + timedelta(minutes=60)

    async def _calendar(cal, _minutes):
        title = "sal-vation" if cal == E["cal_hall"] else "Cubs"
        return [{"start": start.isoformat(), "summary": title}]

    monkeypatch.setattr(ctrl, "_async_calendar_events", _calendar)
    run(ctrl._async_refresh_calendars())
    assert ctrl._room_window_eco["gents"] is False
    assert ctrl._desired_zone("gents") == PRESET_COMFORT


def test_no_booking_ahead_closes_the_windows(monkeypatch):
    ctrl, hass = make_controller()
    _cold_evening(hass)
    _room_temps(hass, 13.0, 13.0, 13.0)
    _upcoming(ctrl, monkeypatch, 60)
    assert ctrl.cal_window["gents"] is True

    async def _nothing(cal, _minutes):
        return []

    monkeypatch.setattr(ctrl, "_async_calendar_events", _nothing)
    run(ctrl._async_refresh_calendars())
    assert not any(ctrl.cal_window[r] for r in ROOMS)


def test_a_calendar_blip_keeps_the_windows(monkeypatch):
    ctrl, hass = make_controller()
    _cold_evening(hass)
    _room_temps(hass, 13.0, 13.0, 13.0)
    _upcoming(ctrl, monkeypatch, 60)

    async def _blip(cal, _minutes):
        return None

    monkeypatch.setattr(ctrl, "_async_calendar_events", _blip)
    run(ctrl._async_refresh_calendars())
    assert ctrl.cal_window["gents"] is True


def test_the_hall_pre_heat_window_alone_does_not_heat_the_rooms():
    # The rooms have their own windows; the hall's opening early for a cold hall
    # is not a reason to heat a warm kitchen.
    ctrl, hass = make_controller()
    _room_temps(hass, 15.0, 15.0, 15.0)
    preheat_window(ctrl, ZA)
    assert set(_desired(ctrl).values()) == {PRESET_ICE}


def test_a_room_window_from_a_finished_session_closes_at_its_end():
    ctrl, hass = make_controller()
    _room_temps(hass, 15.0, 15.0, 15.0)
    ctrl.cal_window["gents"] = True
    ctrl._preheat_open_for["gents"] = ctrl._now() - timedelta(minutes=60)
    ctrl._booking_ended(ZA, handover=False)
    assert ctrl.cal_window["gents"] is False


# --- Learning, per room ----------------------------------------------------------------
def test_a_room_pre_heat_teaches_that_rooms_gain():
    ctrl, hass = make_controller()
    ctrl._numbers["shared_comfort_temp"].native_value = 19.0
    hass.states.set(E["weather"], "cloudy", {"temperature": 8.0})  # cold gate
    _room_temps(hass, 15.0, 14.0, 15.0)
    ctrl.applied["gents"] = PRESET_COMFORT
    ctrl._preset_entry_reason["gents"] = "preheat"
    ctrl._update_warmup_learning()
    assert ctrl._warmup_start["gents"] is not None
    advance(ctrl, 80)
    hass.states.set(GENTS, "heat", {"current_temperature": 19.0})
    ctrl._update_warmup_learning()
    (sample,) = [e for e in _events(ctrl, "warmup_sample") if e["zone"] == "gents"]
    assert sample["rate_key"] == "gents_warmup_rate"
    assert sample["accepted"] is True
    assert ctrl.number("gents_warmup_rate") != 20.0
    assert ctrl.number("kitchen_warmup_rate") == 20.0  # the seed, untouched


def test_a_room_cool_off_teaches_that_rooms_heat_loss():
    ctrl, hass = make_controller()
    ctrl._numbers["shared_comfort_temp"].native_value = 19.0
    hass.states.set(E["weather"], "cloudy", {"temperature": 5.0})
    _room_temps(hass, 15.0, 15.0, 15.0)
    for room in ROOMS:
        ctrl.applied[room] = PRESET_ICE
    ctrl._update_cooloff_learning()
    advance(ctrl, 180)
    ctrl._update_cooloff_learning()
    assert ctrl._cooloff_start["kitchen"] is not None
    for _ in range(12):  # two hours, a quantum at a time
        advance(ctrl, 10)
        ctrl._update_cooloff_learning()
    hass.states.set(KITCHEN, "heat", {"current_temperature": 14.0})
    advance(ctrl, 10)
    ctrl._update_cooloff_learning()
    (sample,) = [e for e in _events(ctrl, "cooloff_sample") if e["zone"] == "kitchen"]
    assert sample["accepted"] is True
    assert ctrl.number("kitchen_heatloss_pct") != 12.0
    assert ctrl.number("gents_heatloss_pct") == 12.0


# --- Applying presets -------------------------------------------------------------------
def _room_sends(hass, climate):
    count = 0
    for c in service_calls(hass, "climate", "set_preset_mode"):
        ids = c["data"].get("entity_id")
        ids = ids if isinstance(ids, list) else [ids]
        if climate in ids:
            count += 1
    return count


def test_each_room_is_applied_and_audited_on_its_own():
    ctrl, hass = make_controller()
    _room_temps(hass, 15.0, 15.0, 15.0)
    motion(ctrl, "gents")
    run(ctrl.async_reconcile())
    assert ctrl.applied["gents"] == PRESET_COMFORT
    assert ctrl.applied["kitchen"] == PRESET_ECO
    assert ctrl.applied["shared"] == PRESET_COMFORT  # the block's warmest room
    presets = {e["zone"]: e for e in _events(ctrl, "preset") if e["zone"] in ROOMS}
    assert presets["gents"]["to"] == PRESET_COMFORT and presets["gents"]["reason"] == "motion"
    assert presets["ladies"]["reason"] == "others_present"
    # Unchanged on the next tick: nothing is re-sent.
    before = _room_sends(hass, GENTS)
    run(ctrl.async_reconcile())
    assert _room_sends(hass, GENTS) == before


def test_a_room_preset_is_resent_after_its_heater_reconnects():
    ctrl, hass = make_controller()
    hass.states.set(GENTS, "unavailable", {})
    run(ctrl.async_reconcile())
    assert ctrl._zone_offline_apply["gents"] is True
    before = _room_sends(hass, GENTS)
    hass.states.set(GENTS, "heat", {})
    run(ctrl.async_reconcile())
    assert _room_sends(hass, GENTS) == before + 1
    assert ctrl._zone_offline_apply["gents"] is False


def test_a_booking_start_records_each_rooms_reading():
    ctrl, hass = make_controller()
    _room_temps(hass, 18.5, 16.0, 17.0)
    run(ctrl.async_reconcile())  # calendar seen off
    booking(ctrl, ZA)
    run(ctrl.async_reconcile())
    (start,) = _events(ctrl, "booking_start")
    assert start["rooms"] == {"kitchen": 18.5, "gents": 16.0, "ladies": 17.0}


def test_diagnostics_carry_each_room():
    ctrl, hass = make_controller()
    _room_temps(hass, 18.5, 16.0, 17.0)
    motion(ctrl, "gents")
    run(ctrl.async_reconcile())
    rooms = ctrl.diagnostics_data()["readings"]["zones"]["shared"]["rooms"]
    assert rooms["gents"]["preset"] == PRESET_COMFORT
    assert rooms["gents"]["temp"] == 16.0
    assert rooms["kitchen"]["reason"] == "others_present"


# --- Unchanged rules -------------------------------------------------------------------
def test_both_alarms_with_no_booking_ice_them():
    ctrl, hass = make_controller()
    _armed(hass)
    motion(ctrl, "kitchen")
    assert set(_desired(ctrl).values()) == {PRESET_ICE}
    assert ctrl._preset_reason["kitchen"] == "alarm"


def test_one_alarm_does_not():
    ctrl, hass = make_controller()
    on(hass, E["alarm_main"])
    _room_temps(hass, 15.0, 15.0, 15.0)
    motion(ctrl, "kitchen")
    assert ctrl._desired_zone("kitchen") == PRESET_COMFORT


@pytest.mark.parametrize("zone", [ZA, ZB])
def test_either_boost_heats_them(zone):
    ctrl, hass = make_controller()
    _armed(hass)
    boost(ctrl, zone)
    assert set(_desired(ctrl).values()) == {PRESET_COMFORT}


def test_an_open_window_ices_them():
    ctrl, hass = make_controller()
    booking(ctrl, ZA)
    motion(ctrl, "hall")
    on(hass, E["shared_window"])
    ctrl.open_since["shared_windows"] = ctrl._now() - timedelta(minutes=30)
    run(ctrl._evaluate_openings())
    assert set(_desired(ctrl).values()) == {PRESET_ICE}


def test_the_season_does_not_matter():
    ctrl, hass = make_controller()
    ctrl.seasonal_lockout = True
    shared_temp(ctrl, 15.0)
    booking(ctrl, ZA)
    motion(ctrl, "hall")
    assert set(_desired(ctrl).values()) == {PRESET_COMFORT}


# --- One ladder for every zone (v1.51.0) ----------------------------------------
# The rooms run through `_desired_zone` itself, so what the hall learned the hard
# way applies to them too.
def test_a_room_frozen_at_target_is_relit_like_the_hall():
    # A heater whose reading has not moved for the relight line while the room
    # waits on ice "warm enough" is relit: a frozen probe never falls below target.
    from custom_components.scout_hut_heating.coordinator import BOOKING_RELIGHT_STALE_MIN

    ctrl, hass = make_controller()
    ctrl._numbers["shared_comfort_temp"].native_value = 19.0
    _cold_evening(hass)
    _room_temps(hass, 19.0, 19.0, 19.0)
    ctrl.cal_window["gents"] = True
    ctrl._track_probe_changes()
    advance(ctrl, BOOKING_RELIGHT_STALE_MIN + 1)
    ctrl.applied["gents"] = PRESET_ICE
    ctrl._preset_reason["gents"] = "booking_warm"
    assert ctrl._desired_zone("gents") == PRESET_COMFORT
    assert ctrl._preset_reason["gents"] == "preheat_stale"


def test_an_eco_booking_heats_only_the_rooms_below_eco():
    # Before v1.51.0 every room went to eco for an ECO booking whatever its
    # reading; now a room already above eco rests on ice, as the hall does.
    ctrl, hass = make_controller()
    _room_temps(hass, 17.0, 15.0, 17.0)
    booking(ctrl, ZA, "sal-vation")
    motion(ctrl, "hall")
    assert _desired(ctrl) == {"kitchen": PRESET_ICE, "gents": PRESET_ECO, "ladies": PRESET_ICE}
    assert ctrl._preset_reason["kitchen"] == "booking_warm"
    assert ctrl._preset_reason["gents"] == "booking_eco"


def test_a_room_with_no_heater_has_no_preset():
    ctrl, _ = make_controller()
    ctrl.config[CONF_SHARED_CLIMATES] = [KITCHEN, GENTS]
    assert ctrl._desired_zone("ladies") is None
