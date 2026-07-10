"""Temporal sequences: 'someone does X, then Y happens' -> state transitions.

Unlike the point-in-time decision tests, each test here drives the controller
through a series of events (with time advanced deterministically) and asserts
the state at every step.
"""

from scout_testkit import (
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
    off,
    on,
    run,
    set_preset_state,
    E,
)


def test_door_opened_then_closed_restores_heating():
    ctrl, hass = make_controller()
    booking(ctrl, ZA)
    motion(ctrl, "hall")
    run(ctrl.async_reconcile())
    assert ctrl.applied[ZA] == PRESET_COMFORT

    on(hass, E["a_door"])           # door opens...
    run(ctrl.async_reconcile())
    assert ctrl.applied[ZA] == PRESET_COMFORT   # ...not long enough yet

    advance(ctrl, 11)               # ...held open 11 minutes
    run(ctrl.async_reconcile())
    assert ctrl.opening_ice[ZA] is True
    assert ctrl.applied[ZA] == PRESET_ICE

    off(hass, E["a_door"])          # door closes
    run(ctrl.async_reconcile())
    assert ctrl.opening_ice[ZA] is False
    assert ctrl.applied[ZA] == PRESET_COMFORT


def test_booking_ends_reverts_then_cools_to_ice():
    ctrl, _ = make_controller()
    booking(ctrl, ZA)
    motion(ctrl, "hall")
    run(ctrl.async_reconcile())
    assert ctrl.applied[ZA] == PRESET_COMFORT

    ctrl.cal_window[ZA] = False     # booking ends, people still around
    run(ctrl.async_reconcile())
    assert ctrl.applied[ZA] == PRESET_ECO

    advance(ctrl, 20)               # everyone leaves, timeout passes
    run(ctrl.async_reconcile())
    assert ctrl.applied[ZA] == PRESET_ICE


def test_motion_outside_booking_then_times_out():
    ctrl, _ = make_controller()
    motion(ctrl, "hall")
    run(ctrl.async_reconcile())
    assert ctrl.applied[ZA] == PRESET_ECO

    advance(ctrl, 20)
    run(ctrl.async_reconcile())
    assert ctrl.applied[ZA] == PRESET_ICE


def test_during_booking_motion_stops_then_returns():
    ctrl, _ = make_controller()
    booking(ctrl, ZA)
    motion(ctrl, "hall")
    run(ctrl.async_reconcile())
    assert ctrl.applied[ZA] == PRESET_COMFORT

    advance(ctrl, 20)               # room empties mid-session
    run(ctrl.async_reconcile())
    assert ctrl.applied[ZA] == PRESET_ECO

    motion(ctrl, "hall")            # someone comes back
    run(ctrl.async_reconcile())
    assert ctrl.applied[ZA] == PRESET_COMFORT


def test_boost_then_expiry_restores_zone_and_shared():
    ctrl, _ = make_controller()
    boost(ctrl, ZA, minutes=30)
    run(ctrl.async_reconcile())
    assert ctrl.applied[ZA] == PRESET_COMFORT
    assert ctrl.applied["shared"] == PRESET_COMFORT

    advance(ctrl, 40)               # boost duration elapses
    run(ctrl.async_reconcile())
    assert ctrl.boost_active(ZA) is False
    assert ctrl.applied[ZA] == PRESET_ICE
    assert ctrl.applied["shared"] == PRESET_ICE


def test_occupied_override_on_then_off():
    ctrl, _ = make_controller()
    ctrl._switches["zone_a_occupied_override"].is_on = True
    run(ctrl.async_reconcile())
    assert ctrl.applied[ZA] == PRESET_ECO

    ctrl._switches["zone_a_occupied_override"].is_on = False
    run(ctrl.async_reconcile())
    assert ctrl.applied[ZA] == PRESET_ICE


def test_alarm_set_then_cleared():
    ctrl, hass = make_controller()
    motion(ctrl, "hall")
    run(ctrl.async_reconcile())
    assert ctrl.applied[ZA] == PRESET_ECO

    on(hass, E["alarm_main"])       # building armed
    run(ctrl.async_reconcile())
    assert ctrl.applied[ZA] == PRESET_ICE

    off(hass, E["alarm_main"])      # disarmed, motion still recent
    run(ctrl.async_reconcile())
    assert ctrl.applied[ZA] == PRESET_ECO


def test_app_change_flags_manual_hold_then_clears():
    ctrl, hass = make_controller()
    booking(ctrl, ZA)
    motion(ctrl, "hall")
    run(ctrl.async_reconcile())
    assert ctrl.applied[ZA] == PRESET_COMFORT

    # Someone overrides the heater in the Rointe app -> preset drifts.
    set_preset_state(hass, "climate.hall_back", "eco")
    advance(ctrl, 2)                # past the 60s settle window
    run(ctrl.async_reconcile())
    assert ctrl.manual_hold[ZA] is True

    # They put it back -> hold releases.
    set_preset_state(hass, "climate.hall_back", "comfort")
    run(ctrl.async_reconcile())
    assert ctrl.manual_hold[ZA] is False


def test_manual_hold_blocks_then_resumes():
    ctrl, _ = make_controller()
    ctrl.manual_hold[ZA] = True
    booking(ctrl, ZA)
    motion(ctrl, "hall")
    run(ctrl.async_reconcile())
    assert ctrl.applied[ZA] is None            # left alone while held

    ctrl.manual_hold[ZA] = False
    run(ctrl.async_reconcile())
    assert ctrl.applied[ZA] == PRESET_COMFORT   # resumes


def test_seasonal_lockout_engages_then_releases():
    ctrl, _ = make_controller()
    booking(ctrl, ZA)
    motion(ctrl, "hall")
    run(ctrl.async_reconcile())
    assert ctrl.applied[ZA] == PRESET_COMFORT

    ctrl.seasonal_lockout = True
    run(ctrl.async_reconcile())
    assert ctrl.applied[ZA] == PRESET_ICE
    assert ctrl.applied["shared"] == PRESET_ICE

    ctrl.seasonal_lockout = False
    run(ctrl.async_reconcile())
    assert ctrl.applied[ZA] == PRESET_COMFORT


def test_water_motion_then_ages_off():
    ctrl, _ = make_controller()
    motion(ctrl, "kitchen")
    run(ctrl.async_reconcile())
    assert ctrl.water_on is True

    advance(ctrl, 70)               # past the 60 min keep-alive
    run(ctrl.async_reconcile())
    assert ctrl.water_on is False


def test_internal_door_through_path_then_internal_closes():
    ctrl, hass = make_controller()
    booking(ctrl, ZA)
    motion(ctrl, "hall")
    run(ctrl.async_reconcile())
    assert ctrl.applied[ZA] == PRESET_COMFORT

    on(hass, E["internal"])
    on(hass, E["a_door"])           # internal + exterior -> heat-loss path
    run(ctrl.async_reconcile())
    assert ctrl.applied[ZA] == PRESET_ICE
    assert ctrl.applied[ZB] == PRESET_ICE

    off(hass, E["internal"])        # close internal door -> path broken
    run(ctrl.async_reconcile())
    assert ctrl.opening_ice[ZA] is False
    assert ctrl.applied[ZA] == PRESET_COMFORT


def test_manual_hold_releases_when_booking_ends():
    # The hold is documented to last "until the booking ends" — verify it
    # actually does, instead of freezing the zone's automation indefinitely.
    ctrl, hass = make_controller()
    booking(ctrl, ZA)
    motion(ctrl, "hall")
    run(ctrl.async_reconcile())
    set_preset_state(hass, "climate.hall_back", "eco")  # app override
    advance(ctrl, 2)
    run(ctrl.async_reconcile())
    assert ctrl.manual_hold[ZA] is True

    ctrl.cal_window[ZA] = False  # booking over (calendar already off)
    run(ctrl.async_reconcile())
    assert ctrl.manual_hold[ZA] is False
    run(ctrl.async_reconcile())
    assert ctrl.applied[ZA] is not None  # automation resumed
