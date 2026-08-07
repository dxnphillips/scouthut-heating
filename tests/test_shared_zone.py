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
    shared_temp,
    E,
)
from custom_components.scout_hut_heating.const import CONF_SHARED_CLIMATES


def test_no_shared_climates_returns_none():
    ctrl, _ = make_controller({CONF_SHARED_CLIMATES: []})
    assert ctrl._desired_shared() is None


def test_empty_is_ice():
    ctrl, _ = make_controller()
    assert ctrl._desired_shared() == PRESET_ICE


def test_hall_booking_warms_cold_shared_to_comfort():
    # A running booking means people are in for a session and will use the
    # kitchen/toilets, so a cold shared zone heats to comfort (not the old eco).
    ctrl, _ = make_controller()
    booking(ctrl, ZA)
    shared_temp(ctrl, 15.0)
    assert ctrl._desired_shared() == PRESET_COMFORT
    assert ctrl._preset_reason["shared"] == "booking"


def test_office_booking_warms_cold_shared_to_comfort():
    ctrl, _ = make_controller()
    booking(ctrl, ZB)
    shared_temp(ctrl, 15.0)
    assert ctrl._desired_shared() == PRESET_COMFORT


def test_warm_shared_under_a_booking_rests_at_eco():
    # Already warm enough — no need to drive comfort; rest at the eco floor.
    ctrl, _ = make_controller()
    booking(ctrl, ZA)
    shared_temp(ctrl, 20.0)  # above shared_comfort_temp (19.5)
    assert ctrl._desired_shared() == PRESET_ECO
    assert ctrl._preset_reason["shared"] == "shared_warm"


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
    shared_temp(ctrl, 15.0)
    assert ctrl._desired_shared() == PRESET_COMFORT  # kitchen in use, cold


def test_shared_area_motion_warms_cold_shared_to_comfort():
    # Motion in a shared PIR (gents) means someone is actually in the block.
    ctrl, _ = make_controller()
    motion(ctrl, "gents")
    shared_temp(ctrl, 15.0)
    assert ctrl._desired_shared() == PRESET_COMFORT
    assert ctrl._preset_reason["shared"] == "shared_motion"


def test_hall_only_motion_keeps_shared_at_eco():
    # Nobody in the shared block (only the hall) — the lighter eco floor, so a
    # cleaner in the hall does not warm the toilets to comfort.
    ctrl, _ = make_controller()
    motion(ctrl, "hall")
    shared_temp(ctrl, 15.0)
    assert ctrl._desired_shared() == PRESET_ECO
    assert ctrl._preset_reason["shared"] == "motion"


def test_season_does_not_ice_shared():
    # The season no longer gates heating: shared warms whatever the flag says.
    ctrl, _ = make_controller()
    ctrl.seasonal_lockout = True
    booking(ctrl, ZA)
    shared_temp(ctrl, 15.0)
    assert ctrl._desired_shared() == PRESET_COMFORT
    assert ctrl._preset_reason["shared"] == "booking"


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


def test_shared_preset_resent_after_heaters_reconnect():
    from scout_testkit import E, run, service_calls

    ctrl, hass = make_controller()
    run(ctrl.async_reconcile())  # heaters have no state yet: offline apply
    assert ctrl._shared_offline_apply is True

    def shared_sends():
        count = 0
        for c in service_calls(hass, "climate", "set_preset_mode"):
            ids = c["data"].get("entity_id")
            ids = ids if isinstance(ids, list) else [ids]
            if E["shared"][0] in ids:
                count += 1
        return count

    before = shared_sends()
    for eid in E["shared"]:
        hass.states.set(eid, "heat", {})  # all shared heaters back online
    run(ctrl.async_reconcile())
    assert shared_sends() == before + 1  # unchanged preset re-sent once
    assert ctrl._shared_offline_apply is False
    run(ctrl.async_reconcile())
    assert shared_sends() == before + 1  # and only once
