"""Boost lifecycle."""

from datetime import timedelta

from custom_components.scout_hut_heating.coordinator import ROINTE_COMFORT_MAX
from scout_testkit import PRESET_COMFORT, ZA, ZB, make_controller, run


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


# --- Boost drives ABOVE comfort (a boost means "still too cold") -------------
def test_boost_drives_above_comfort():
    ctrl, _ = make_controller()
    base = ctrl.number("hall_comfort_temp")
    assert ctrl._drive_comfort_target(ZA) == base  # no boost -> plain comfort
    run(ctrl.async_boost(ZA))
    assert ctrl._drive_comfort_target(ZA) == base + ctrl.number("boost_offset")


def test_boost_offset_clamped_to_rointe_max():
    ctrl, _ = make_controller()
    ctrl._numbers["hall_comfort_temp"].native_value = 29.5
    ctrl._numbers["boost_offset"].native_value = 5.0
    run(ctrl.async_boost(ZA))
    assert ctrl._drive_comfort_target(ZA) == ROINTE_COMFORT_MAX  # 29.5+5 -> 30 cap


def test_hall_desired_setpoint_follows_the_boost():
    # The destrat fans chase the boosted target so they keep delivering heat.
    ctrl, _ = make_controller()
    ctrl.applied[ZA] = PRESET_COMFORT
    base = ctrl.number("hall_comfort_temp")
    assert ctrl._hall_desired_setpoint() == base
    run(ctrl.async_boost(ZA))
    assert ctrl._hall_desired_setpoint() == base + ctrl.number("boost_offset")


def test_shared_drive_target_follows_a_zone_boost():
    ctrl, _ = make_controller()
    base = ctrl.number("shared_comfort_temp")
    assert ctrl._drive_comfort_target("shared") == base
    run(ctrl.async_boost(ZB))  # office boost -> shared follows
    assert ctrl._drive_comfort_target("shared") == base + ctrl.number("boost_offset")
