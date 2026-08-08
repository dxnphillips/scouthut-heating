"""Drive self-validation (Q20): does the loop know its commands are working?

Two independent, notification-only checks on top of the drive loop:
(a) setpoint read-back — the heater's REPORTED setpoint must match what we
    pushed once a settle window has passed (catches the phantom-push class);
(b) ceiling cross-check — heat requested and short of target, yet neither floor
    nor ceiling moves over a long window, means nothing is responding anywhere.
"""

from datetime import timedelta

from custom_components.scout_hut_heating.const import CONF_CEILING_TEMP
from custom_components.scout_hut_heating.coordinator import (
    DRIVE_NO_RESPONSE_MINUTES,
    DRIVE_SETTLE_MINUTES,
    DRIVE_STARTUP_GRACE_MINUTES,
)


def _past_startup(ctrl):
    """Simulate the integration having been up past the drive self-check grace."""
    ctrl._started_at = ctrl._now() - timedelta(minutes=DRIVE_STARTUP_GRACE_MINUTES + 1)
from scout_testkit import PRESET_COMFORT, ZA, E, make_controller

CLIMATE = "climate.hall_back"
CEIL = "sensor.ceiling"


def _audit(ctrl, kind):
    return [e for e in ctrl.audit._events if e.get("event") == kind]


# --- (a) Setpoint read-back ------------------------------------------------
def _settled_push(ctrl, hass, pushed, reported):
    now = ctrl._now()
    _past_startup(ctrl)
    ctrl._drive_pushed[CLIMATE] = pushed
    ctrl._drive_pushed_at[CLIMATE] = now - timedelta(minutes=DRIVE_SETTLE_MINUTES + 1)
    hass.states.set(CLIMATE, "heat", {"temperature": reported, "current_temperature": 18.0})
    return now


def test_matching_setpoint_is_not_flagged():
    ctrl, hass = make_controller()
    now = _settled_push(ctrl, hass, pushed=22.0, reported=22.0)
    ctrl._check_setpoint_readback(CLIMATE, 22.0, now)
    assert CLIMATE not in ctrl._drive_rejected


def test_diverging_setpoint_after_settle_is_flagged_and_notifies():
    ctrl, hass = make_controller()
    now = _settled_push(ctrl, hass, pushed=22.0, reported=19.5)  # never adopted
    ctrl._check_setpoint_readback(CLIMATE, 22.0, now)
    assert CLIMATE in ctrl._drive_rejected
    ctrl._update_drive_reject_alarm()
    assert ctrl._drive_reject_notified
    assert len(_audit(ctrl, "drive_setpoint_rejected")) == 1


def test_startup_grace_abstains_even_after_settle():
    # Post-restart: the settle window has elapsed but the integration only just
    # started, so the read-back must abstain (the Rointe cloud lags after boot).
    ctrl, hass = make_controller()
    now = ctrl._now()
    ctrl._started_at = now - timedelta(minutes=1)  # just started
    ctrl._drive_pushed[CLIMATE] = 22.0
    ctrl._drive_pushed_at[CLIMATE] = now - timedelta(minutes=DRIVE_SETTLE_MINUTES + 1)
    hass.states.set(CLIMATE, "heat", {"temperature": 19.5})  # not adopted yet
    ctrl._check_setpoint_readback(CLIMATE, 22.0, now)
    assert CLIMATE not in ctrl._drive_rejected  # grace holds it off
    # Once past the grace, the same unadopted setpoint IS flagged.
    _past_startup(ctrl)
    ctrl._check_setpoint_readback(CLIMATE, 22.0, now)
    assert CLIMATE in ctrl._drive_rejected


def test_actively_heating_mismatch_is_not_flagged():
    # A heater short of the pushed setpoint but actively HEATING has accepted the
    # command (its live setpoint just lags our push through the cloud while the
    # drive staircases up). Only an IDLE short heater is the phantom-push fault.
    ctrl, hass = make_controller()
    now = _settled_push(ctrl, hass, pushed=22.0, reported=21.5)  # 0.5 short
    hass.states.set(CLIMATE, "heat", {"temperature": 21.5, "hvac_action": "heating"})
    ctrl._check_setpoint_readback(CLIMATE, 22.0, now)
    assert CLIMATE not in ctrl._drive_rejected
    # Same mismatch but IDLE -> genuinely not accepting -> flagged.
    hass.states.set(CLIMATE, "heat", {"temperature": 21.5, "hvac_action": "idle"})
    ctrl._check_setpoint_readback(CLIMATE, 22.0, now)
    assert CLIMATE in ctrl._drive_rejected


def test_divergence_within_settle_window_abstains():
    ctrl, hass = make_controller()
    now = ctrl._now()
    ctrl._drive_pushed_at[CLIMATE] = now - timedelta(minutes=DRIVE_SETTLE_MINUTES - 2)
    hass.states.set(CLIMATE, "heat", {"temperature": 19.5})
    ctrl._check_setpoint_readback(CLIMATE, 22.0, now)
    assert CLIMATE not in ctrl._drive_rejected


def test_unreadable_setpoint_abstains():
    ctrl, hass = make_controller()
    now = ctrl._now()
    ctrl._drive_pushed_at[CLIMATE] = now - timedelta(minutes=DRIVE_SETTLE_MINUTES + 1)
    hass.states.set(CLIMATE, "heat", {"current_temperature": 18.0})  # no 'temperature'
    ctrl._check_setpoint_readback(CLIMATE, 22.0, now)
    assert CLIMATE not in ctrl._drive_rejected


def test_recovers_when_a_new_value_is_pushed():
    """A fresh push resets the settle clock, so the heater is not flagged until
    the new value has had its own window to land."""
    ctrl, hass = make_controller()
    now = _settled_push(ctrl, hass, pushed=22.0, reported=19.5)
    ctrl._check_setpoint_readback(CLIMATE, 22.0, now)
    assert CLIMATE in ctrl._drive_rejected
    # A new push just now: within the settle window, so abstain.
    ctrl._drive_pushed_at[CLIMATE] = now
    ctrl._check_setpoint_readback(CLIMATE, 21.5, now)
    assert CLIMATE not in ctrl._drive_rejected


def test_reject_alarm_clears_on_recovery():
    ctrl, hass = make_controller()
    now = _settled_push(ctrl, hass, pushed=22.0, reported=19.5)
    ctrl._check_setpoint_readback(CLIMATE, 22.0, now)
    ctrl._update_drive_reject_alarm()
    assert ctrl._drive_reject_notified
    # Device adopts the value.
    hass.states.set(CLIMATE, "heat", {"temperature": 22.0})
    ctrl._check_setpoint_readback(CLIMATE, 22.0, now)
    ctrl._update_drive_reject_alarm()
    assert not ctrl._drive_reject_notified


# --- (b) Ceiling cross-check (no response) ---------------------------------
def _no_resp_ctrl(floor=17.0, ceiling=20.0):
    ctrl, hass = make_controller(config_overrides={CONF_CEILING_TEMP: CEIL})
    _past_startup(ctrl)
    ctrl.applied[ZA] = PRESET_COMFORT
    for eid in E["hall"]:
        hass.states.set(eid, "heat", {"current_temperature": floor})
    hass.states.set(CEIL, str(ceiling))
    return ctrl, hass


def test_no_response_over_window_alerts():
    ctrl, hass = _no_resp_ctrl()
    start = ctrl._now()
    ctrl._drive_response_ref = (start - timedelta(minutes=DRIVE_NO_RESPONSE_MINUTES + 1), 17.0, 20.0)
    ctrl._update_drive_no_response(start)  # floor/ceiling unchanged from ref
    assert ctrl._drive_noresp_notified
    assert len(_audit(ctrl, "drive_no_response")) == 1


def test_ceiling_movement_resets_and_no_alert():
    ctrl, hass = _no_resp_ctrl(ceiling=21.0)  # ceiling rose vs the 20.0 ref
    start = ctrl._now()
    ctrl._drive_response_ref = (start - timedelta(minutes=DRIVE_NO_RESPONSE_MINUTES + 1), 17.0, 20.0)
    ctrl._update_drive_no_response(start)
    assert not ctrl._drive_noresp_notified


def test_floor_movement_resets_and_no_alert():
    ctrl, hass = _no_resp_ctrl(floor=17.5)  # floor rose vs the 17.0 ref
    start = ctrl._now()
    ctrl._drive_response_ref = (start - timedelta(minutes=DRIVE_NO_RESPONSE_MINUTES + 1), 17.0, 20.0)
    ctrl._update_drive_no_response(start)
    assert not ctrl._drive_noresp_notified


def test_not_short_no_alert():
    ctrl, hass = _no_resp_ctrl(floor=21.0)  # at/above target -> not short
    start = ctrl._now()
    ctrl._drive_response_ref = (start - timedelta(minutes=DRIVE_NO_RESPONSE_MINUTES + 1), 21.0, 20.0)
    ctrl._update_drive_no_response(start)
    assert not ctrl._drive_noresp_notified
    assert ctrl._drive_response_ref is None


def test_no_ceiling_sensor_abstains():
    ctrl, hass = make_controller()  # no ceiling mapped
    ctrl.applied[ZA] = PRESET_COMFORT
    for eid in E["hall"]:
        hass.states.set(eid, "heat", {"current_temperature": 17.0})
    start = ctrl._now()
    ctrl._drive_response_ref = (start - timedelta(minutes=DRIVE_NO_RESPONSE_MINUTES + 1), 17.0, None)
    ctrl._update_drive_no_response(start)
    assert not ctrl._drive_noresp_notified


def test_no_response_first_tick_arms_the_reference():
    ctrl, hass = _no_resp_ctrl()
    assert ctrl._drive_response_ref is None
    ctrl._update_drive_no_response(ctrl._now())
    assert ctrl._drive_response_ref is not None
    assert not ctrl._drive_noresp_notified  # no alert on the arming tick
