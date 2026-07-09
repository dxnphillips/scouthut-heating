"""Boost lifecycle."""

from datetime import timedelta

from scout_testkit import PRESET_COMFORT, ZA, make_controller, run


def test_async_boost_activates():
    ctrl, _ = make_controller()
    run(ctrl.async_boost(ZA))
    assert ctrl.boost_active(ZA) is True
    assert ctrl.applied[ZA] == PRESET_COMFORT


def test_cancel_boost_clears():
    ctrl, _ = make_controller()
    run(ctrl.async_boost(ZA))
    run(ctrl.async_cancel_boost(ZA))
    assert ctrl.boost_active(ZA) is False


def test_boost_expiry_clears_and_requests_reconcile():
    ctrl, _ = make_controller()
    ctrl.boost_until[ZA] = ctrl._now() - timedelta(minutes=1)
    ctrl._expire_boosts()
    assert ctrl.boost_until[ZA] is None
    assert ctrl._reconcile_pending is True


def test_boost_active_still_true_before_expiry():
    ctrl, _ = make_controller()
    ctrl.boost_until[ZA] = ctrl._now() + timedelta(minutes=5)
    assert ctrl.boost_active(ZA) is True


def test_boost_minutes_parsed_from_select():
    ctrl, _ = make_controller()
    ctrl._selects["boost_duration"].current_option = "90 min"
    assert ctrl.boost_minutes() == 90


def test_boost_minutes_default():
    ctrl, _ = make_controller()
    assert ctrl.boost_minutes() == 60
