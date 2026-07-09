"""Door / window / internal-door opening -> ice behaviour."""

from datetime import timedelta

from scout_testkit import PRESET_ICE, ZA, ZB, booking, make_controller, motion, on, run, E


def _held(ctrl, group_key, minutes=11):
    ctrl.open_since[group_key] = ctrl._now() - timedelta(minutes=minutes)


def test_hall_door_held_open_ices_hall():
    ctrl, hass = make_controller()
    on(hass, E["a_door"])
    _held(ctrl, "zone_a_doors")
    run(ctrl._evaluate_openings())
    assert ctrl.opening_ice[ZA] is True


def test_hall_door_briefly_open_does_not_ice():
    ctrl, hass = make_controller()
    on(hass, E["a_door"])
    ctrl.open_since["zone_a_doors"] = ctrl._now() - timedelta(minutes=1)
    run(ctrl._evaluate_openings())
    assert ctrl.opening_ice[ZA] is False


def test_hall_window_held_open_ices_hall():
    ctrl, hass = make_controller()
    on(hass, E["a_window"])
    _held(ctrl, "zone_a_windows")
    run(ctrl._evaluate_openings())
    assert ctrl.opening_ice[ZA] is True


def test_office_door_held_open_ices_office():
    ctrl, hass = make_controller()
    on(hass, E["b_door"])
    _held(ctrl, "zone_b_doors")
    run(ctrl._evaluate_openings())
    assert ctrl.opening_ice[ZB] is True


def test_shared_window_held_open_ices_shared():
    ctrl, hass = make_controller()
    on(hass, E["shared_window"])
    _held(ctrl, "shared_windows")
    run(ctrl._evaluate_openings())
    assert ctrl.opening_ice["shared"] is True


def test_all_closed_clears_opening_ice():
    ctrl, _ = make_controller()
    ctrl.opening_ice[ZA] = True
    run(ctrl._evaluate_openings())  # nothing open
    assert ctrl.opening_ice[ZA] is False


def test_internal_plus_exterior_through_path_ices_both():
    ctrl, hass = make_controller()
    on(hass, E["internal"])
    on(hass, E["a_door"])
    run(ctrl._evaluate_openings())
    assert ctrl.opening_ice[ZA] is True
    assert ctrl.opening_ice[ZB] is True


def test_opening_ice_beats_active_booking():
    ctrl, hass = make_controller()
    booking(ctrl, ZA)
    motion(ctrl, "hall")
    on(hass, E["a_door"])
    _held(ctrl, "zone_a_doors")
    run(ctrl.async_reconcile())
    assert ctrl.applied[ZA] == PRESET_ICE
