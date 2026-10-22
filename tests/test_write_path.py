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


def _preset_calls(hass, climate, preset):
    return [
        c for c in service_calls(hass, "climate", "set_preset_mode")
        if climate in _ids(c) and c["data"].get("preset_mode") == preset
    ]


def test_a_failed_preset_write_is_re_sent_on_the_next_tick():
    # 2026-10-22 11:46Z: the office's ice write failed as the alarm was set,
    # `applied` recorded ice anyway, and nothing re-sent it, so the office kept
    # heating an armed, empty building for three hours.
    ctrl, hass = make_controller()
    office = E["office"][0]
    hass.states.set(office, "heat", {"current_temperature": 17.0})
    motion(ctrl, "office")
    run(ctrl.async_reconcile())
    assert ctrl.applied["zone_b"] == PRESET_COMFORT
    hass.states.set(E["alarm_office"], "on")  # armed away: the office goes to ice
    hass.services.fail_when = (
        lambda d, s, data: d == "climate" and s == "set_preset_mode" and office in _ids({"data": data})
    )
    run(ctrl.async_reconcile())
    assert ctrl.applied["zone_b"] == PRESET_ICE
    assert len(_preset_calls(hass, office, PRESET_ICE)) == 1
    assert [e["heater"] for e in _events(ctrl, "write_failed")] == [office]
    run(ctrl.async_reconcile())  # still failing: re-sent, audited once
    assert len(_preset_calls(hass, office, PRESET_ICE)) == 2
    assert len(_events(ctrl, "write_failed")) == 1
    hass.services.fail_when = None
    run(ctrl.async_reconcile())  # lands
    assert len(_preset_calls(hass, office, PRESET_ICE)) == 3
    assert _events(ctrl, "write_recovered")
    run(ctrl.async_reconcile())  # and is not re-sent again
    assert len(_preset_calls(hass, office, PRESET_ICE)) == 3
    # One `preset` event for the change, none for the re-sends.
    assert len([e for e in _events(ctrl, "preset") if e["zone"] == "zone_b" and e["to"] == PRESET_ICE]) == 1


def test_a_failed_shared_preset_write_is_re_sent_on_the_next_tick():
    ctrl, hass = make_controller()
    kitchen = E["shared"][0]
    for climate in E["shared"]:
        hass.states.set(climate, "heat", {"current_temperature": 15.0})
    motion(ctrl, "kitchen")
    run(ctrl.async_reconcile())
    shared_now = ctrl.applied["shared"]
    assert shared_now is not None and shared_now != PRESET_ICE
    hass.states.set(E["alarm_main"], "on")
    hass.states.set(E["alarm_office"], "on")
    hass.services.fail_when = (
        lambda d, s, data: d == "climate" and s == "set_preset_mode" and kitchen in _ids({"data": data})
    )
    run(ctrl.async_reconcile())
    assert ctrl.applied["shared"] == PRESET_ICE
    sent = len(_preset_calls(hass, kitchen, PRESET_ICE))
    hass.services.fail_when = None
    run(ctrl.async_reconcile())
    assert len(_preset_calls(hass, kitchen, PRESET_ICE)) == sent + 1
    run(ctrl.async_reconcile())
    assert len(_preset_calls(hass, kitchen, PRESET_ICE)) == sent + 1


# --- Withdrawal: audited; an insane probe HOLDS the staircase ---------------
def test_insane_probe_withdrawal_holds_the_staircase_and_is_audited():
    _wire_numbers()
    ctrl, hass = make_controller()
    # Comfort pinned high: with two probes the sanity rule (> 4 under the
    # median) can only fire while the zone AVERAGE — what the heat gate reads
    # since v1.47.0 — is still short of target if the target sits well above.
    ctrl._numbers["hall_comfort_temp"].native_value = 24.0
    # hall_back inside the escalation band (v1.50.0) so it can wind two steps;
    # hall_front far short, so the zone average stays clear of the approach hold.
    _hall_comfort(ctrl, hass, {HB: 23.0, HF: 22.0})
    run(ctrl.async_reconcile())
    _age_steps(ctrl)
    hass.states.set(HB, "heat", {"current_temperature": 23.5})  # responded
    run(ctrl.async_reconcile())
    held = ctrl._drive_stair[HB]
    assert held >= 1.0
    # One batch: hall_front catches up to target while hall_back's reading drops
    # to 19.0 — now > 4 under the zone median, so the sanity rule fires on a
    # reading that is merely glitched, with the zone still in comfort.
    hass.states.set(HF, "heat", {"current_temperature": 24.0})
    hass.states.set(HB, "heat", {"current_temperature": 19.0})
    run(ctrl.async_reconcile())
    target = ctrl.number("hall_comfort_temp")
    assert _pushed(hass, _num(HB)) == target and _landed(hass, HB) == target
    assert ctrl._drive_stair[HB] == held  # held, not forfeited
    assert HB not in ctrl._drive_driven
    withdrawn = _events(ctrl, "drive_withdrawn")
    assert withdrawn and withdrawn[-1]["heater"] == HB and withdrawn[-1]["reason"] == "insane"
    assert withdrawn[-1]["stair"] == held
    # The reading recovers (back inside the sanity band, still short of target):
    # it re-enters with the held overdrive, not from zero.
    hass.states.set(HB, "heat", {"current_temperature": 21.0})
    run(ctrl.async_reconcile())
    assert HB in ctrl._drive_driven
    assert _pushed(hass, _num(HB)) >= target + held - 1e-9


def test_leaving_comfort_withdraws_the_number_only_and_is_not_audited():
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
    # The routine end of a driven episode is NOT audited: it fires per heater on
    # every exit from comfort (56 events in 41 h of field running, 11 % of the
    # bounded log) and the `preset` event beside it already says what happened.
    # Only the silent FAULT withdrawals (`unreadable`, `insane`) keep their mark.
    assert [e["reason"] for e in _events(ctrl, "drive_withdrawn")] == []


# --- Target drop: the staircase restarts instead of riding the old overdrive -
def test_a_target_drop_resets_the_staircase():
    _wire_numbers()
    ctrl, hass = make_controller()
    # Boost target = comfort + boost_offset = 21.5. hall_back a degree short of it
    # (inside the escalation band, v1.50.0) winds two steps; hall_front is left
    # far short so the zone average stays clear of the approach hold now and of
    # the warm line once the boost expires.
    _hall_comfort(ctrl, hass, {HB: 20.5, HF: 18.5})
    run(ctrl.async_boost(ZA))  # target = comfort + boost_offset
    run(ctrl.async_reconcile())
    _age_steps(ctrl)
    hass.states.set(HB, "heat", {"current_temperature": 21.0})
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


def test_a_hold_that_rounds_down_mid_session_keeps_a_short_heaters_stair():
    # 2026-10-21 16:50Z, a Wednesday booking in a hall that started the
    # afternoon at 11.9: the booking hold sat on the 0.5 rounding line (margin
    # 0.255 at outdoor 8.0, 0.232 at 9.0 with k 7.15 and gain 11.67), the
    # weather ticked up a degree, the target rounded 19.5 -> 19.0, and the old
    # rule reset +2.5-3.0 of overdrive on all four heaters while their probes
    # sat at 17.5-18.0. The room fell 18.9 -> 17.8 over the next hour.
    _wire_numbers()
    ctrl, hass = make_controller()
    for key, value in (
        ("hall_comfort_temp", 19.0),
        ("zone_a_heatloss_pct", 7.15),
        ("zone_a_warmup_rate", 11.67),
        ("zone_a_warmup_rate_fans", 11.67),
        ("booking_hold_cap", 1.5),
    ):
        ctrl._numbers[key].native_value = value
    booking(ctrl, ZA)
    motion(ctrl, "hall")
    hass.states.set(E["weather"], "cloudy", {"temperature": 8.0})
    hass.states.set(HB, "heat", {"current_temperature": 18.0})
    hass.states.set(HF, "heat", {"current_temperature": 17.5})
    run(ctrl.async_reconcile())
    assert ctrl._drive_target[HB] == 19.5  # 19.0 + a hold that rounds up
    # The overdrive the afternoon's climb had committed by then, its last step
    # just taken (so this tick isolates the drop from the next step).
    for climate in (HB, HF):
        ctrl._drive_stair[climate] = 2.5
        ctrl._drive_step_at[climate] = dt_util.utcnow()
    hass.states.set(E["weather"], "cloudy", {"temperature": 9.0})
    run(ctrl.async_reconcile())
    assert ctrl._drive_target[HB] == 19.0  # the hold rounded down
    assert ctrl._drive_stair[HB] == 2.5 and ctrl._drive_stair[HF] == 2.5
    assert _pushed(hass, _num(HB)) == 21.5  # the drop lowers it; nothing more
    drops = _events(ctrl, "drive_target_drop")
    assert {e["heater"] for e in drops} == {HB, HF}
    assert all(e["reset"] is False for e in drops)
    assert next(e for e in drops if e["heater"] == HB)["probe"] == 18.0


def test_a_target_drop_resets_only_the_heater_that_has_reached_it():
    _wire_numbers()
    ctrl, hass = make_controller()
    _hall_comfort(ctrl, hass, {HB: 18.0, HF: 18.0})
    run(ctrl.async_reconcile())
    target = ctrl.number("hall_comfort_temp")
    # A higher target was in force a moment ago (a boost or a hold) and both
    # heaters carry overdrive; hall_back's probe has reached the lower target,
    # hall_front's has not.
    for climate in (HB, HF):
        ctrl._drive_target[climate] = target + 0.5
        ctrl._drive_stair[climate] = 1.5
    hass.states.set(HB, "heat", {"current_temperature": target})
    hass.states.set(HF, "heat", {"current_temperature": target - 1.5})
    run(ctrl.async_reconcile())
    assert ctrl._drive_stair[HB] == 0.0
    assert ctrl._drive_stair[HF] == 1.5
    by_heater = {e["heater"]: e for e in _events(ctrl, "drive_target_drop")}
    assert by_heater[HB]["reset"] is True and by_heater[HF]["reset"] is False


# --- Approach guard: an insane probe is left out of the zone average --------
def test_zone_average_for_the_approach_guard_excludes_an_insane_probe():
    _wire_numbers()
    ctrl, hass = make_controller()
    target = ctrl.number("hall_comfort_temp")
    # hall_front a degree short (inside the escalation band, v1.50.0) takes its
    # first step; hall_back far short takes none (a saturated element).
    _hall_comfort(ctrl, hass, {HB: 14.5, HF: target - 1.0})
    run(ctrl.async_reconcile())
    assert ctrl._drive_stair[HF] == 0.5 and ctrl._drive_stair[HB] == 0.0
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
