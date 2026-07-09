"""The Shelly-facing fan actuator (`_async_ensure_fans`).

These lock in the hard hardware rules:
  * a live direction change goes through the reverse button, exactly once per
    reconcile, with the grace window armed;
  * without a mapped direction relay (or reverse button) a live reversal is
    never attempted — a blind re-press every reconcile would otherwise cycle
    the motor through a full 45 s reversal sequence forever;
  * the direction relay is only ever written while the master is off, with a
    settle before the master is energised.
"""

from __future__ import annotations

import pytest

from custom_components.scout_hut_heating import coordinator as C
from custom_components.scout_hut_heating.const import (
    CONF_FAN_DIRECTION,
    CONF_FAN_MASTER,
    CONF_FAN_REVERSE,
)
from scout_testkit import make_controller, off, on, run, service_calls

MASTER = "switch.fan_master"
DIRECTION = "switch.fan_direction"
REVERSE = "button.fan_reverse"


@pytest.fixture(autouse=True)
def _no_settle(monkeypatch):
    """Skip the real contactor settle so the tests stay instant."""
    monkeypatch.setattr(C, "FAN_DIRECTION_SETTLE", 0)


def fan_controller(*, with_direction=True, with_reverse=True):
    overrides = {CONF_FAN_MASTER: MASTER}
    if with_direction:
        overrides[CONF_FAN_DIRECTION] = DIRECTION
    if with_reverse:
        overrides[CONF_FAN_REVERSE] = REVERSE
    ctrl, hass = make_controller(config_overrides=overrides)
    off(hass, MASTER)
    if with_direction:
        off(hass, DIRECTION)  # forward
    return ctrl, hass


def button_presses(hass):
    return service_calls(hass, "button", "press")


def direction_writes(hass):
    return [
        c
        for c in hass.services.calls
        if c["domain"] == "switch" and c["data"].get("entity_id") == DIRECTION
    ]


def test_live_direction_change_presses_reverse_once_and_arms_grace():
    ctrl, hass = fan_controller()
    on(hass, MASTER)  # running forward, want reverse
    run(ctrl._async_ensure_fans(True, "reverse"))
    assert len(button_presses(hass)) == 1
    assert not direction_writes(hass)  # never written while the master is on
    assert ctrl.fan_action_grace_until is not None


def test_no_direction_relay_never_live_reverses():
    # Master + reverse button mapped but no direction relay: the running
    # direction is unknowable, so the reconciler must keep the fans running
    # as they are instead of blind-pressing reverse on every pass.
    ctrl, hass = fan_controller(with_direction=False)
    on(hass, MASTER)
    for _ in range(3):  # several reconciles must not accumulate presses
        run(ctrl._async_ensure_fans(True, "reverse"))
    assert not button_presses(hass)
    assert ctrl.fan_on is True
    assert ctrl.fan_action_grace_until is None


def test_no_reverse_button_never_live_reverses():
    ctrl, hass = fan_controller(with_reverse=False)
    on(hass, MASTER)  # running forward, want reverse, but no button to press
    run(ctrl._async_ensure_fans(True, "reverse"))
    assert not direction_writes(hass)
    assert ctrl.fan_on is True
    assert ctrl.fan_action_grace_until is None


def test_start_from_off_presets_direction_before_master():
    ctrl, hass = fan_controller()
    run(ctrl._async_ensure_fans(True, "reverse"))
    calls = [
        (c["service"], c["data"].get("entity_id"))
        for c in hass.services.calls
        if c["domain"] == "switch"
    ]
    assert calls == [("turn_on", DIRECTION), ("turn_on", MASTER)]
    assert not button_presses(hass)  # stopped start never uses the button
    assert ctrl.fan_on is True and ctrl.fan_direction == "reverse"


def test_start_from_off_matching_direction_skips_relay_write():
    ctrl, hass = fan_controller()
    on(hass, DIRECTION)  # already reverse
    run(ctrl._async_ensure_fans(True, "reverse"))
    assert not direction_writes(hass)
    master_calls = [
        c for c in hass.services.calls if c["data"].get("entity_id") == MASTER
    ]
    assert [c["service"] for c in master_calls] == ["turn_on"]


def test_off_opens_master():
    ctrl, hass = fan_controller()
    on(hass, MASTER)
    ctrl.fan_on = True
    run(ctrl._async_ensure_fans(False, None))
    master_calls = [
        c for c in hass.services.calls if c["data"].get("entity_id") == MASTER
    ]
    assert [c["service"] for c in master_calls] == ["turn_off"]
    assert ctrl.fan_on is False and ctrl.fan_master_expected is False


# --- Season changeover (which regime is active) --------------------------------

def test_summer_follows_seasonal_lockout():
    ctrl, _ = fan_controller()
    assert ctrl._summer_active() is False  # heating season, default switches
    ctrl.seasonal_lockout = True
    assert ctrl._summer_active() is True  # lockout engaged -> cooling regime
    ctrl.seasonal_lockout = False
    assert ctrl._summer_active() is False  # autumn: back to destratification


def test_manual_summer_mode_forces_cooling_regardless_of_season():
    ctrl, _ = fan_controller()
    ctrl._switches["summer_mode"].is_on = True
    assert ctrl._summer_active() is True


def test_follow_season_can_be_disabled():
    ctrl, _ = fan_controller()
    ctrl._switches["summer_follows_season"].is_on = False
    ctrl.seasonal_lockout = True
    assert ctrl._summer_active() is False


# --- Fan awareness in the optimum-start learning --------------------------------

def test_warmup_rate_key_follows_fan_availability():
    from scout_testkit import ZA, ZB

    ctrl, _ = fan_controller()
    # Winter, fans enabled: predict warm-ups with the fan-assisted rate.
    assert ctrl._warmup_rate_key(ZA) == "zone_a_warmup_rate_fans"
    # Summer regime: fans blow a cooling breeze, not destratified heat.
    ctrl.seasonal_lockout = True
    assert ctrl._warmup_rate_key(ZA) == "zone_a_warmup_rate"
    ctrl.seasonal_lockout = False
    ctrl._switches["fans_enabled"].is_on = False
    assert ctrl._warmup_rate_key(ZA) == "zone_a_warmup_rate"
    # The office has no fans, ever.
    assert ctrl._warmup_rate_key(ZB) == "zone_b_warmup_rate"


def test_no_fan_hardware_uses_base_rate():
    from scout_testkit import ZA

    ctrl, _ = make_controller()  # no fan master mapped
    assert ctrl._warmup_rate_key(ZA) == "zone_a_warmup_rate"


def test_fans_running_requires_real_power_when_metered():
    from custom_components.scout_hut_heating.const import CONF_FAN_O1_POWER

    ctrl, hass = make_controller(
        config_overrides={CONF_FAN_MASTER: MASTER, CONF_FAN_O1_POWER: "sensor.fan_power"}
    )
    on(hass, MASTER)
    hass.states.set("sensor.fan_power", "2.5")  # dial at zero: idle transformer
    assert ctrl._fans_running() is False
    hass.states.set("sensor.fan_power", "120")
    assert ctrl._fans_running() is True
    off(hass, MASTER)
    assert ctrl._fans_running() is False


# --- No pre-cooling: the breeze needs someone to cool ---------------------------

def test_preheat_window_does_not_start_the_summer_breeze():
    from scout_testkit import ZA, booking

    ctrl, _ = fan_controller()
    booking(ctrl, ZA)  # pre-heat window / upcoming event, hall still empty
    assert ctrl._cooling_occupied() is False


def test_running_event_keeps_the_breeze_without_motion():
    from scout_testkit import E

    ctrl, hass = fan_controller()
    on(hass, E["cal_hall"])  # event underway; seated group outside PIR view
    assert ctrl._cooling_occupied() is True


def test_hall_motion_starts_the_breeze():
    from scout_testkit import motion

    ctrl, _ = fan_controller()
    motion(ctrl, "hall")
    assert ctrl._cooling_occupied() is True
