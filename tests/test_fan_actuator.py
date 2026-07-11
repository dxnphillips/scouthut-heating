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
    from scout_testkit import ZA, preheat_window

    ctrl, _ = fan_controller()
    preheat_window(ctrl, ZA)  # upcoming event, hall still empty
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


# --- Fault inference: the latch must be reachable --------------------------------

def test_unexpected_master_off_latches_fault_instead_of_hammering():
    from scout_testkit import PRESET_COMFORT, ZA, advance

    ctrl, hass = fan_controller()
    ctrl.applied[ZA] = PRESET_COMFORT  # heat demand (fallback path), sensor lost
    run(ctrl._reconcile_fans())  # winter run-on-loss: fans commanded on
    turn_ons = [c for c in hass.services.calls if c["service"] == "turn_on"
                and c["data"].get("entity_id") == MASTER]
    assert len(turn_ons) == 1 and ctrl.fan_master_expected is True

    off(hass, MASTER)  # Shelly latched its own fault / someone hit the switch
    run(ctrl._reconcile_fans())
    turn_ons = [c for c in hass.services.calls if c["service"] == "turn_on"
                and c["data"].get("entity_id") == MASTER]
    assert len(turn_ons) == 1  # NOT re-commanded: that would re-arm the Shelly

    advance(ctrl, 2)  # past FAN_FAULT_GRACE
    run(ctrl._reconcile_fans())
    assert ctrl.fan_fault_latched is True
    turn_ons = [c for c in hass.services.calls if c["service"] == "turn_on"
                and c["data"].get("entity_id") == MASTER]
    assert len(turn_ons) == 1  # still exactly one command ever sent


def test_reverse_press_gives_up_and_latches_after_repeated_failures():
    ctrl, hass = fan_controller()
    on(hass, MASTER)  # running forward, want reverse; script absent: relay never moves
    for _ in range(3):
        run(ctrl._async_ensure_fans(True, "reverse"))
        ctrl.fan_action_grace_until = None  # grace expired, nothing changed
    assert len(button_presses(hass)) == 3
    run(ctrl._async_ensure_fans(True, "reverse"))
    assert len(button_presses(hass)) == 3  # no fourth press
    assert ctrl.fan_fault_latched is True


def test_wall_switch_power_cycle_recovers_automatically():
    # Wall switch kills the Shelly (entities unavailable), power returns with
    # outputs defaulting OFF: the controller must re-establish the wanted
    # state on the next tick — no fault latch, no deadlock, no re-arm needed.
    from scout_testkit import PRESET_COMFORT, ZA

    ctrl, hass = fan_controller()
    ctrl.applied[ZA] = PRESET_COMFORT  # heat demand via the fallback path
    run(ctrl._reconcile_fans())  # fans commanded on
    assert ctrl.fan_master_expected is True

    hass.states.set(MASTER, "unavailable")  # wall switch off: Shelly dead
    hass.states.set(DIRECTION, "unavailable")
    run(ctrl._reconcile_fans())  # must wait quietly, not latch or command
    assert ctrl.fan_fault_latched is False

    off(hass, MASTER)  # power back: Shelly boots with outputs OFF
    off(hass, DIRECTION)
    run(ctrl._reconcile_fans())  # same-tick recovery
    assert ctrl.fan_fault_latched is False
    assert hass.states.get(MASTER).state == "on"  # re-commanded


def test_manual_master_kill_still_latches_with_fault_boolean_mapped():
    # A mapped-but-clear fault boolean must not disable the unexpected-off
    # inference: the Shelly's script cannot see a manual master kill.
    from custom_components.scout_hut_heating.const import CONF_FAN_FAULT
    from scout_testkit import PRESET_COMFORT, ZA, advance

    ctrl, hass = make_controller(
        config_overrides={
            CONF_FAN_MASTER: MASTER,
            CONF_FAN_DIRECTION: DIRECTION,
            CONF_FAN_REVERSE: REVERSE,
            CONF_FAN_FAULT: "binary_sensor.fan_fault",
        }
    )
    off(hass, MASTER)
    off(hass, DIRECTION)
    off(hass, "binary_sensor.fan_fault")  # script healthy, no fault published
    ctrl.applied[ZA] = PRESET_COMFORT
    run(ctrl._reconcile_fans())  # fans on
    off(hass, MASTER)  # killed in the app — entities stayed available
    run(ctrl._reconcile_fans())
    advance(ctrl, 2)
    run(ctrl._reconcile_fans())
    assert ctrl.fan_fault_latched is True  # inferred latch fired
    assert ctrl.fan_fault_effective is True  # OR'd into the effective fault


# --- Heat demand: stale power sensors must not fall through to presets ----------

def _power_ctrl():
    from custom_components.scout_hut_heating.const import CONF_ROINTE_POWER

    ctrl, hass = make_controller(
        config_overrides={CONF_ROINTE_POWER: ["sensor.rointe_power"]}
    )
    return ctrl, hass


def _age(hass, eid, minutes):
    from datetime import timedelta

    st = hass.states.get(eid)
    st.last_updated -= timedelta(minutes=minutes)
    st.last_reported = st.last_updated


def test_stale_zero_power_reading_is_still_no_demand():
    from scout_testkit import PRESET_ECO, ZA

    ctrl, hass = _power_ctrl()
    hass.states.set("sensor.rointe_power", "0")
    _age(hass, "sensor.rointe_power", 600)  # cloud silent all summer at 0 W
    ctrl.applied[ZA] = PRESET_ECO  # a heating preset is applied...
    assert ctrl._heat_demand() is False  # ...but 0 W is 0 W, not demand


def test_fresh_power_above_threshold_is_demand():
    ctrl, hass = _power_ctrl()
    hass.states.set("sensor.rointe_power", "450")
    assert ctrl._heat_demand() is True


def test_frozen_high_reading_cannot_assert_demand():
    ctrl, hass = _power_ctrl()
    hass.states.set("sensor.rointe_power", "450")
    _age(hass, "sensor.rointe_power", 600)  # frozen mid-heating weeks ago
    assert ctrl._heat_demand() is False


def test_no_readable_sensors_falls_back_to_presets():
    from scout_testkit import PRESET_COMFORT, ZA

    ctrl, hass = _power_ctrl()
    hass.states.set("sensor.rointe_power", "unavailable")
    ctrl.applied[ZA] = PRESET_COMFORT
    assert ctrl._heat_demand() is True


# --- Hot-breeze guard + ceiling freshness (availability, not report age) ---------

def test_breeze_guard_holds_fans_and_releases_when_air_cools():
    from custom_components.scout_hut_heating.const import CONF_CEILING_TEMP
    from scout_testkit import E, make_controller, on, run

    ctrl, hass = make_controller(
        config_overrides={CONF_FAN_MASTER: MASTER, CONF_CEILING_TEMP: "sensor.ceiling"}
    )
    off(hass, MASTER)
    ctrl.seasonal_lockout = True  # summer regime via follows-season
    on(hass, E["cal_hall"])  # event running: occupied
    for eid in E["hall"]:
        hass.states.set(eid, "heat", {"current_temperature": 30.0})
    hass.states.set("sensor.ceiling", "38.0")  # mix = 0.75*30 + 0.25*38 = 32

    run(ctrl._reconcile_fans())
    assert ctrl.fan_breeze_hot is True
    assert not ctrl.fan_on  # held: warm and occupied, but the breeze would not help
    assert any(e["event"] == "breeze_holdoff" for e in ctrl.audit.to_list())
    assert ctrl.fan_overheated is False  # distinct from the hard 35 cutoff

    # The building vents: mixed air falls below the release band -> breeze resumes.
    for eid in E["hall"]:
        hass.states.set(eid, "heat", {"current_temperature": 26.0})
    hass.states.set("sensor.ceiling", "30.0")  # mix = 27 <= 28
    run(ctrl._reconcile_fans())
    assert ctrl.fan_breeze_hot is False
    assert ctrl.fan_on is True


def test_quiet_ceiling_sensor_is_fresh_while_available():
    # The ceiling H&T only reports on a 0.5 degC change: hours of silence mean
    # "unchanged", not "lost". Freshness is availability, not report age.
    from datetime import timedelta

    from homeassistant.util import dt as dt_util

    from custom_components.scout_hut_heating.const import CONF_CEILING_TEMP
    from scout_testkit import E, make_controller

    ctrl, hass = make_controller(
        config_overrides={CONF_FAN_MASTER: MASTER, CONF_CEILING_TEMP: "sensor.ceiling"}
    )
    for eid in E["hall"]:
        hass.states.set(eid, "heat", {"current_temperature": 18.0})
    hass.states.set("sensor.ceiling", "22.0")
    st = hass.states.get("sensor.ceiling")
    st.last_updated = st.last_reported = dt_util.utcnow() - timedelta(hours=5)

    ctrl._fan_target()
    assert ctrl.fan_sensor_stale is False
    assert ctrl.fan_dt == pytest.approx(4.0)

    hass.states.set("sensor.ceiling", "unavailable")  # actually dead
    ctrl._fan_target()
    assert ctrl.fan_sensor_stale is True
