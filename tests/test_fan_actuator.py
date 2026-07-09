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
