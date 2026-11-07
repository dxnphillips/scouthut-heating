"""A Boost ends when its building is armed and left (v1.51.2).

Field 2026-11-06: Boost pressed at 19:37:53Z in a Thursday booking that ended
at 20:30Z; the hall was empty from ~20:34Z and the alarm set, but a Boost sits
above the alarm in the ladder, so all four hall radiators (stair +1.0) and the
kitchen and toilets kept heating toward 21 until the boost ran out at
21:07:53Z, and the alarm's ice landed on the next tick. The alarm's arm time
was not in the audit at all.
"""

from datetime import timedelta

from scout_testkit import (
    E,
    PRESET_COMFORT,
    PRESET_ICE,
    ZA,
    ZB,
    booking,
    end_booking,
    hall_temp,
    make_controller,
    run,
)


def _events(ctrl, name):
    return [e for e in ctrl.audit.to_list() if e.get("event") == name]


def _arm(hass, state="armed_away"):
    hass.states.set(E["alarm_main"], state)


def _boosted_in_open_building():
    ctrl, hass = make_controller()
    _arm(hass, "disarmed")
    hall_temp(ctrl, 18.5)
    run(ctrl.async_boost(ZA))
    assert ctrl.applied[ZA] == PRESET_COMFORT
    return ctrl, hass


def test_arming_and_leaving_ends_the_boost():
    ctrl, hass = _boosted_in_open_building()
    _arm(hass)
    run(ctrl.async_reconcile())
    assert not ctrl.boost_active(ZA)
    assert ctrl.applied[ZA] == PRESET_ICE
    (evt,) = _events(ctrl, "boost_cancelled")
    assert evt["reason"] == "alarm" and evt["zone"] == ZA


def test_the_arm_is_audited_once_without_its_arming_step():
    ctrl, hass = _boosted_in_open_building()
    _arm(hass, "arming")
    run(ctrl.async_reconcile())
    _arm(hass)
    run(ctrl.async_reconcile())
    (evt,) = _events(ctrl, "alarm_change")
    assert (evt["panel"], evt["previous"], evt["state"]) == ("main", "disarmed", "armed_away")
    _arm(hass, "unavailable")  # a blip is not a change
    run(ctrl.async_reconcile())
    _arm(hass)
    run(ctrl.async_reconcile())
    assert len(_events(ctrl, "alarm_change")) == 1


def test_a_boost_pressed_into_an_armed_building_is_a_pre_warm():
    ctrl, hass = make_controller()
    _arm(hass)
    hall_temp(ctrl, 15.0)
    run(ctrl.async_boost(ZA))
    run(ctrl.async_reconcile())
    assert ctrl.boost_active(ZA)
    assert ctrl.applied[ZA] == PRESET_COMFORT
    # They arrive and disarm, then leave and arm: that one ends it.
    _arm(hass, "disarmed")
    run(ctrl.async_reconcile())
    _arm(hass)
    run(ctrl.async_reconcile())
    assert not ctrl.boost_active(ZA)


def test_a_running_booking_keeps_its_boost_until_it_ends():
    ctrl, hass = _boosted_in_open_building()
    booking(ctrl, ZA)
    _arm(hass)  # the group stepped out mid-booking: expected back
    run(ctrl.async_reconcile())
    assert ctrl.boost_active(ZA)
    end_booking(ctrl, ZA)
    run(ctrl.async_reconcile())
    assert not ctrl.boost_active(ZA)


def test_a_night_arm_is_not_leaving():
    ctrl, hass = _boosted_in_open_building()
    _arm(hass, "armed_night")
    run(ctrl.async_reconcile())
    assert ctrl.boost_active(ZA)


def test_the_office_boost_follows_the_office_panel():
    ctrl, hass = make_controller()
    hass.states.set(E["alarm_office"], "disarmed")
    run(ctrl.async_boost(ZB))
    _arm(hass)  # the MAIN panel: not the office's
    run(ctrl.async_reconcile())
    assert ctrl.boost_active(ZB)
    hass.states.set(E["alarm_office"], "armed_away")
    run(ctrl.async_reconcile())
    assert not ctrl.boost_active(ZB)


def test_the_remote_flag_survives_a_restart():
    ctrl, hass = make_controller()
    _arm(hass)
    run(ctrl.async_boost(ZA))
    saved = ctrl._state_snapshot()
    again, hass2 = make_controller()
    hass2.states.set(E["alarm_main"], "armed_away")

    async def _load():
        return saved

    again._store.async_load = _load
    run(again._async_restore_state())
    run(again.async_reconcile())
    assert again.boost_active(ZA)


def test_a_store_without_the_flag_keeps_the_old_behaviour():
    ctrl, hass = make_controller()
    hass.states.set(E["alarm_main"], "armed_away")
    until = ctrl._now() + timedelta(minutes=60)

    async def _load():
        return {"boost_until": {ZA: until.isoformat()}, "audit": [], "trace": []}

    ctrl._store.async_load = _load
    run(ctrl._async_restore_state())
    run(ctrl.async_reconcile())
    assert ctrl.boost_active(ZA)
