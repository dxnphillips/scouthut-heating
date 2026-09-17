"""The heater write path after the Rointe/Nexa findings (2026-09-16, Q25 PR 1).

On the Rointe integration ``set_preset_mode`` copies a stale CACHED number into
the live setpoint, so every intended value is now LANDED with
``climate.set_temperature``; every heater write blocks and a raise is audited
(``write_failed`` / ``write_recovered``) and retried; an insane-probe withdrawal
holds the staircase instead of zeroing it and is audited (``drive_withdrawn``);
a target drop (boost expiry) resets the staircase (``drive_target_drop``); the
approach guard's zone average leaves an insane probe out.
"""

from datetime import timedelta

from homeassistant.util import dt as dt_util

from scout_testkit import (
    E,
    PRESET_COMFORT,
    PRESET_ECO,
    PRESET_ICE,
    ZA,
    booking,
    make_controller,
    motion,
    run,
    service_calls,
    set_registry,
)

HB, HF = "climate.hall_back", "climate.hall_front"


def _wire_numbers():
    """Registry so every heater has comfort + eco numbers on its device."""
    heaters = {
        HB: "dhb",
        HF: "dhf",
        "climate.office": "dof",
        "climate.kitchen": "dk",
        "climate.gents": "dg",
        "climate.ladies": "dl",
    }
    entries, devices = {}, {}
    for climate, dev in heaters.items():
        name = climate.split(".")[1]
        entries[dev] = [
            climate,
            f"number.{name}_comfort_temperature",
            f"number.{name}_eco_temperature",
        ]
        devices[climate] = dev
    set_registry(entries_by_device=entries, entity_devices=devices)


def _num(climate):
    return f"number.{climate.split('.')[1]}_comfort_temperature"


def _ids(call):
    ids = call["data"].get("entity_id")
    return ids if isinstance(ids, list) else [ids]


def _pushed(hass, number):
    val = None
    for c in service_calls(hass, "number", "set_value"):
        if number in _ids(c):
            val = c["data"].get("value")
    return val


def _landed(hass, climate):
    """Last live setpoint written to a heater via climate.set_temperature."""
    val = None
    for c in service_calls(hass, "climate", "set_temperature"):
        if climate in _ids(c):
            val = c["data"].get("temperature")
    return val


def _landings(hass, climate):
    return [c for c in service_calls(hass, "climate", "set_temperature") if climate in _ids(c)]


def _events(ctrl, name):
    return [e for e in ctrl.audit.to_list() if e.get("event") == name]


def _hall_comfort(ctrl, hass, temps):
    booking(ctrl, ZA)
    motion(ctrl, "hall")
    ctrl._numbers["booking_hold_cap"].native_value = 0  # isolate drive mechanics
    hass.states.set(E["weather"], "cloudy", {"temperature": 6.0})
    for climate, t in temps.items():
        hass.states.set(climate, "heat", {"current_temperature": t})


def _age_steps(ctrl):
    for climate in (HB, HF):
        ctrl._drive_step_at[climate] = dt_util.utcnow() - timedelta(hours=1)


# --- Landing: set_temperature with OUR value, never a preset re-assert -------
def test_drive_push_lands_the_value_with_set_temperature_not_a_preset_reassert():
    _wire_numbers()
    ctrl, hass = make_controller()
    _hall_comfort(ctrl, hass, {HB: 18.0, HF: 18.0})
    run(ctrl.async_reconcile())
    target = ctrl.number("hall_comfort_temp")
    value = _pushed(hass, _num(HB))
    assert value > target  # driven above the plain target
    # The same value is landed on the LIVE setpoint — the number alone never
    # moves it, and a preset re-assert would copy the stale cached number.
    assert _landed(hass, HB) == value
    per_heater_reasserts = [
        c for c in service_calls(hass, "climate", "set_preset_mode")
        if c["data"].get("entity_id") == HB
    ]
    assert not per_heater_reasserts


def test_every_heater_write_blocks():
    _wire_numbers()
    ctrl, hass = make_controller()
    _hall_comfort(ctrl, hass, {HB: 18.0, HF: 18.0})
    run(ctrl.async_reconcile())
    heater_calls = [
        c for c in hass.services.calls
        if c["domain"] in ("number", "climate")
    ]
    assert heater_calls
    assert all(c["blocking"] for c in heater_calls)


def test_hall_eco_apply_lands_the_eco_value_and_the_eco_low_repush():
    _wire_numbers()
    ctrl, hass = make_controller()
    motion(ctrl, "office")  # someone elsewhere -> hall rests at ordinary eco
    run(ctrl.async_reconcile())
    assert ctrl.applied[ZA] == PRESET_ECO
    eco = ctrl.number("hall_eco_temp")
    assert _landed(hass, HB) == eco and _landed(hass, HF) == eco
    # A keyword booking starts on an already-eco hall: the backstop re-push of
    # the eco number must LAND the eco-low value too (a changed number alone
    # left the radiator at 16 on this hardware).
    booking(ctrl, ZA, title="test")
    run(ctrl.async_reconcile())
    assert ctrl.applied[ZA] == PRESET_ECO
    assert _landed(hass, HB) == ctrl.number("hall_eco_low_temp")


def test_undriven_comfort_lands_the_comfort_value():
    _wire_numbers()
    ctrl, hass = make_controller()
    ctrl._switches["drive_to_target"].is_on = False
    _hall_comfort(ctrl, hass, {HB: 18.0, HF: 18.0})
    run(ctrl.async_reconcile())
    assert ctrl.applied[ZA] == PRESET_COMFORT
    assert _landed(hass, HB) == ctrl.number("hall_comfort_temp")


def test_ice_lands_nothing():
    # The cached anti-frost value is a constant 7: the preset alone is right.
    _wire_numbers()
    ctrl, hass = make_controller()
    run(ctrl.async_reconcile())
    assert ctrl.applied[ZA] == PRESET_ICE
    assert not service_calls(hass, "climate", "set_temperature")


def test_last_will_lands_the_plain_target_for_a_comfort_zone():
    _wire_numbers()
    ctrl, hass = make_controller()
    _hall_comfort(ctrl, hass, {HB: 18.0, HF: 18.0})
    run(ctrl.async_reconcile())
    comfort = ctrl.number("hall_comfort_temp")
    assert _landed(hass, HB) > comfort
    run(ctrl.async_drive_reset())
    assert _pushed(hass, _num(HB)) == comfort
    assert _landed(hass, HB) == comfort  # the live setpoint, not just the number


# --- Failures are surfaced and retried, never believed ----------------------
def test_a_failed_heater_write_is_audited_once_and_retried():
    _wire_numbers()
    ctrl, hass = make_controller()
    hass.services.fail_when = (
        lambda d, s, data: d == "climate" and s == "set_temperature" and data.get("entity_id") == HB
    )
    _hall_comfort(ctrl, hass, {HB: 18.0, HF: 18.0})
    run(ctrl.async_reconcile())
    failed = _events(ctrl, "write_failed")
    assert len(failed) == 1
    assert failed[0]["heater"] == HB and failed[0]["service"] == "climate.set_temperature"
    assert HB not in ctrl._drive_pushed  # not believed: retried next tick
    assert _pushed(hass, _num(HF)) > ctrl.number("hall_comfort_temp")  # the loop carried on
    run(ctrl.async_reconcile())  # still failing: one audit event, not one per tick
    assert len(_events(ctrl, "write_failed")) == 1
    assert len(_landings(hass, HB)) >= 2  # it WAS retried
    hass.services.fail_when = None
    run(ctrl.async_reconcile())
    assert HB in ctrl._drive_pushed
    assert _events(ctrl, "write_recovered")


# --- Withdrawal: audited; an insane probe HOLDS the staircase ---------------
def test_insane_probe_withdrawal_holds_the_staircase_and_is_audited():
    _wire_numbers()
    ctrl, hass = make_controller()
    _hall_comfort(ctrl, hass, {HB: 18.0, HF: 18.0})
    run(ctrl.async_reconcile())
    _age_steps(ctrl)
    for c in (HB, HF):
        hass.states.set(c, "heat", {"current_temperature": 18.5})  # both responded
    run(ctrl.async_reconcile())
    held = ctrl._drive_stair[HB]
    assert held >= 1.0
    # hall_front freeze-jumps +4.5 in one batch: hall_back is now > 4 under the
    # zone median and the sanity rule fires on a merely LATE probe.
    hass.states.set(HF, "heat", {"current_temperature": 23.0})
    run(ctrl.async_reconcile())
    target = ctrl.number("hall_comfort_temp")
    assert _pushed(hass, _num(HB)) == target and _landed(hass, HB) == target
    assert ctrl._drive_stair[HB] == held  # held, not forfeited
    assert HB not in ctrl._drive_driven
    withdrawn = _events(ctrl, "drive_withdrawn")
    assert withdrawn and withdrawn[-1]["heater"] == HB and withdrawn[-1]["reason"] == "insane"
    assert withdrawn[-1]["stair"] == held
    # The probe catches up (still short of target, back inside the sanity band):
    # it re-enters with the held overdrive, not from zero.
    hass.states.set(HB, "heat", {"current_temperature": 19.0})
    run(ctrl.async_reconcile())
    assert HB in ctrl._drive_driven
    assert _pushed(hass, _num(HB)) >= target + held - 1e-9


def test_leaving_comfort_withdraws_the_number_only_and_is_audited():
    _wire_numbers()
    ctrl, hass = make_controller()
    _hall_comfort(ctrl, hass, {HB: 18.0, HF: 18.0})
    run(ctrl.async_reconcile())
    landings_before = len(_landings(hass, HB))
    # The room is now warm -> the booking lands on ice; the ice preset owns the
    # live setpoint, so only the comfort NUMBER is restored (no set_temperature).
    for c in (HB, HF):
        hass.states.set(c, "heat", {"current_temperature": 24.0})
    run(ctrl.async_reconcile())
    assert ctrl.applied[ZA] == PRESET_ICE
    assert _pushed(hass, _num(HB)) == ctrl.number("hall_comfort_temp")
    assert len(_landings(hass, HB)) == landings_before
    assert ctrl._drive_stair[HB] == 0.0
    withdrawn = _events(ctrl, "drive_withdrawn")
    assert withdrawn and withdrawn[-1]["reason"] == "not_comfort"


# --- Target drop: the staircase restarts instead of riding the old overdrive -
def test_a_target_drop_resets_the_staircase():
    _wire_numbers()
    ctrl, hass = make_controller()
    _hall_comfort(ctrl, hass, {HB: 18.0, HF: 18.0})
    run(ctrl.async_boost(ZA))  # target = comfort + boost_offset
    run(ctrl.async_reconcile())
    _age_steps(ctrl)
    for c in (HB, HF):
        hass.states.set(c, "heat", {"current_temperature": 18.5})
    run(ctrl.async_reconcile())
    assert ctrl._drive_stair[HB] >= 1.0
    boosted = _pushed(hass, _num(HB))
    # The boost expires while the booking keeps the zone in comfort.
    ctrl.boost_until[ZA] = ctrl._now() - timedelta(minutes=1)
    run(ctrl.async_reconcile())
    comfort = ctrl.number("hall_comfort_temp")
    assert ctrl._drive_stair[HB] == 0.0
    assert _pushed(hass, _num(HB)) <= comfort + 0.5  # at most the feedforward head-start
    assert _pushed(hass, _num(HB)) < boosted
    drops = [e for e in _events(ctrl, "drive_target_drop") if e["heater"] == HB]
    assert drops and drops[-1]["target"] == comfort and drops[-1]["stair"] >= 1.0


# --- Approach guard: an insane probe is left out of the zone average --------
def test_zone_average_for_the_approach_guard_excludes_an_insane_probe():
    _wire_numbers()
    ctrl, hass = make_controller()
    target = ctrl.number("hall_comfort_temp")
    _hall_comfort(ctrl, hass, {HB: 14.5, HF: 15.5})
    run(ctrl.async_reconcile())  # first step on both
    _age_steps(ctrl)
    # hall_front catches up to within a step of target; hall_back's probe stays
    # frozen at 14.5, now > 4 under the median -> insane. With it excluded the
    # zone average (= hall_front) has reached the top of the approach, so
    # hall_front is approach-held rather than wound up another step.
    hass.states.set(HF, "heat", {"current_temperature": target - 0.5})
    run(ctrl.async_reconcile())
    assert HF in ctrl._drive_approach
    assert ctrl._drive_stair[HF] <= 0.5 + 1e-9
    assert _events(ctrl, "drive_withdrawn")[-1]["heater"] == HB
