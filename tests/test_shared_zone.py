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
    E,
)
from custom_components.scout_hut_heating.const import CONF_SHARED_CLIMATES


def test_no_shared_climates_returns_none():
    ctrl, _ = make_controller({CONF_SHARED_CLIMATES: []})
    assert ctrl._desired_shared() is None


def test_empty_is_ice():
    ctrl, _ = make_controller()
    assert ctrl._desired_shared() == PRESET_ICE


def test_hall_booking_makes_shared_eco():
    ctrl, _ = make_controller()
    booking(ctrl, ZA)
    assert ctrl._desired_shared() == PRESET_ECO


def test_office_booking_makes_shared_eco():
    ctrl, _ = make_controller()
    booking(ctrl, ZB)
    assert ctrl._desired_shared() == PRESET_ECO


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
    assert ctrl._desired_shared() == PRESET_ECO


def test_motion_anywhere_makes_shared_eco():
    ctrl, _ = make_controller()
    motion(ctrl, "gents")
    assert ctrl._desired_shared() == PRESET_ECO


def test_seasonal_lockout_is_ice():
    ctrl, _ = make_controller()
    ctrl.seasonal_lockout = True
    booking(ctrl, ZA)
    assert ctrl._desired_shared() == PRESET_ICE


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
