"""Drive-to-target wired into the reconciler: per-heater setpoint pushes + safety net."""

from datetime import timedelta

from homeassistant.util import dt as dt_util

from scout_testkit import (
    PRESET_COMFORT,
    ZA,
    booking,
    make_controller,
    motion,
    run,
    set_registry,
    E,
)


def _wire_numbers():
    """Registry so every hall/office/shared heater has a comfort number on its device."""
    heaters = {
        "climate.hall_back": "dhb",
        "climate.hall_front": "dhf",
        "climate.office": "dof",
        "climate.kitchen": "dk",
        "climate.gents": "dg",
        "climate.ladies": "dl",
    }
    entries = {}
    devices = {}
    for climate, dev in heaters.items():
        num = f"number.{climate.split('.')[1]}_comfort_temperature"
        entries[dev] = [climate, num]
        devices[climate] = dev
    set_registry(entries_by_device=entries, entity_devices=devices)


def _comfort_number(climate):
    return f"number.{climate.split('.')[1]}_comfort_temperature"


def _pushed(hass, number):
    """Last value written to a number entity via number.set_value."""
    val = None
    for c in hass.services.calls:
        if c["domain"] == "number" and c["service"] == "set_value":
            ids = c["data"].get("entity_id")
            ids = ids if isinstance(ids, list) else [ids]
            if number in ids:
                val = c["data"].get("value")
    return val


def _hall_comfort(ctrl, hass, temps):
    """Put the hall into comfort with the given per-heater probe temps."""
    booking(ctrl, ZA)
    motion(ctrl, "hall")
    hass.states.set(E["weather"], "cloudy", {"temperature": 6.0})  # cold -> feedforward
    for climate, t in temps.items():
        hass.states.set(climate, "heat", {"current_temperature": t})


# --- Core: drives above target when short ------------------------------------
def test_drives_setpoint_above_target_when_room_is_short():
    _wire_numbers()
    ctrl, hass = make_controller()
    _hall_comfort(ctrl, hass, {"climate.hall_back": 18.0, "climate.hall_front": 18.0})
    run(ctrl.async_reconcile())
    assert ctrl.applied[ZA] == PRESET_COMFORT
    target = ctrl.number("hall_comfort_temp")
    assert _pushed(hass, _comfort_number("climate.hall_back")) > target


def test_drive_reasserts_the_comfort_preset_so_the_setpoint_lands():
    # A Rointe only adopts a changed comfort number when comfort is re-applied,
    # so a drive push must be followed by a set_preset_mode on that heater —
    # otherwise the boost never reaches the radiator (v1.14.3 fix).
    _wire_numbers()
    ctrl, hass = make_controller()
    _hall_comfort(ctrl, hass, {"climate.hall_back": 18.0, "climate.hall_front": 18.0})
    run(ctrl.async_reconcile())
    reasserts = [
        c for c in hass.services.calls
        if c["domain"] == "climate" and c["service"] == "set_preset_mode"
        and "climate.hall_back" in (c["data"].get("entity_id") if isinstance(c["data"].get("entity_id"), list) else [c["data"].get("entity_id")])
        and c["data"].get("preset_mode") == PRESET_COMFORT
    ]
    assert reasserts  # the driven heater had comfort re-applied after the push


def test_per_heater_independent_drive():
    # One end cold, the other on target: only the cold one is driven up.
    _wire_numbers()
    ctrl, hass = make_controller()
    _hall_comfort(ctrl, hass, {"climate.hall_back": 18.0, "climate.hall_front": 19.5})
    run(ctrl.async_reconcile())
    target = ctrl.number("hall_comfort_temp")
    assert _pushed(hass, _comfort_number("climate.hall_back")) > target
    # The on-target heater gets at most a one-step head-start, never driven hard.
    assert _pushed(hass, _comfort_number("climate.hall_front")) <= target + 0.5


# --- Never past the cap -------------------------------------------------------
def test_setpoint_never_exceeds_the_cap():
    _wire_numbers()
    ctrl, hass = make_controller()
    _hall_comfort(ctrl, hass, {"climate.hall_back": 10.0, "climate.hall_front": 10.0})
    cap = ctrl.number("hall_comfort_temp") + ctrl.number("drive_max_offset")
    for _ in range(60):  # many steps of a very cold room
        for climate in ("climate.hall_back", "climate.hall_front"):
            ctrl._drive_step_at[climate] = dt_util.utcnow() - timedelta(hours=1)
        run(ctrl.async_reconcile())
    assert _pushed(hass, _comfort_number("climate.hall_back")) <= cap


# --- Safety net: freshness ----------------------------------------------------
def test_stale_probe_withdraws_to_plain_target():
    _wire_numbers()
    ctrl, hass = make_controller()
    _hall_comfort(ctrl, hass, {"climate.hall_back": 18.0, "climate.hall_front": 18.0})
    # Age one heater's report beyond the staleness window.
    old = dt_util.utcnow() - timedelta(hours=3)
    st = hass.states.get("climate.hall_back")
    st.last_reported = old
    st.last_updated = old
    run(ctrl.async_reconcile())
    target = ctrl.number("hall_comfort_temp")
    assert _pushed(hass, _comfort_number("climate.hall_back")) == target  # not boosted


# --- Safety net: cross-probe sanity ------------------------------------------
def test_insane_low_probe_is_not_driven_on():
    _wire_numbers()
    ctrl, hass = make_controller()
    # One probe reads absurdly low vs the others -> treat as a glitch, don't drive it.
    _hall_comfort(ctrl, hass, {"climate.hall_back": 2.0, "climate.hall_front": 19.0})
    run(ctrl.async_reconcile())
    target = ctrl.number("hall_comfort_temp")
    assert _pushed(hass, _comfort_number("climate.hall_back")) == target  # withdrawn


# --- Switch off: no driving ---------------------------------------------------
def test_switch_off_does_not_overdrive():
    _wire_numbers()
    ctrl, hass = make_controller()
    ctrl._switches["drive_to_target"].is_on = False
    _hall_comfort(ctrl, hass, {"climate.hall_back": 18.0, "climate.hall_front": 18.0})
    run(ctrl.async_reconcile())
    target = ctrl.number("hall_comfort_temp")
    # The base comfort push still lands the plain target; nothing is driven above it.
    assert (_pushed(hass, _comfort_number("climate.hall_back")) or target) <= target


# --- Last will: reset restores plain targets ---------------------------------
def test_last_will_reset_restores_targets():
    _wire_numbers()
    ctrl, hass = make_controller()
    _hall_comfort(ctrl, hass, {"climate.hall_back": 18.0, "climate.hall_front": 18.0})
    run(ctrl.async_reconcile())
    assert _pushed(hass, _comfort_number("climate.hall_back")) > ctrl.number("hall_comfort_temp")
    run(ctrl.async_drive_reset())
    assert _pushed(hass, _comfort_number("climate.hall_back")) == ctrl.number("hall_comfort_temp")
    assert ctrl._drive_pushed == {}


# --- Hands off manual control -------------------------------------------------
def test_hands_off_during_manual_hold():
    _wire_numbers()
    ctrl, hass = make_controller()
    _hall_comfort(ctrl, hass, {"climate.hall_back": 18.0, "climate.hall_front": 18.0})
    ctrl.manual_hold[ZA] = True  # user took manual control
    run(ctrl.async_reconcile())
    # No setpoint was pushed to the hall comfort numbers at all (left as-is).
    assert _pushed(hass, _comfort_number("climate.hall_back")) is None


# --- Driving must not be mistaken for manual control (v1.14.0 regression) -----
def test_driving_does_not_false_flag_manual_control():
    _wire_numbers()
    ctrl, hass = make_controller()
    _hall_comfort(ctrl, hass, {"climate.hall_back": 18.0, "climate.hall_front": 18.0})
    # Already in comfort before the tick (as after a restart), so no fresh preset
    # apply -> the drift settle window is not active. The heaters report a null
    # preset and the OLD setpoint (19.5) while the drive pushes a boosted value.
    ctrl.applied[ZA] = PRESET_COMFORT
    ctrl.expected_preset[ZA] = PRESET_COMFORT
    for climate in ("climate.hall_back", "climate.hall_front"):
        hass.states.set(climate, "heat", {"current_temperature": 18.0, "temperature": 19.5})
    ctrl.manual_hold[ZA] = True  # a hold the bug had latched
    run(ctrl.async_reconcile())
    assert ctrl.manual_hold[ZA] is False  # no false drift, and the stale hold cleared


# --- Deadlocked stale hold clears (v1.14.2 regression) ------------------------
def test_stale_hold_with_no_expected_clears_when_driving():
    # Reproduces the v1.14.0->v1.14.1 deadlock: a persisted manual_hold with
    # expected_preset None (the hold blocked the apply that would set it) and a
    # live booking. With driving on it must clear rather than stick forever.
    _wire_numbers()
    ctrl, hass = make_controller()
    booking(ctrl, ZA)
    ctrl.manual_hold[ZA] = True
    ctrl.expected_preset[ZA] = None
    for climate in ("climate.hall_back", "climate.hall_front"):
        hass.states.set(climate, "heat", {"current_temperature": 19.0})
    run(ctrl.async_reconcile())
    assert ctrl.manual_hold[ZA] is False


# --- Office driven to its own new slider --------------------------------------
def test_office_driven_to_office_comfort_slider():
    _wire_numbers()
    ctrl, hass = make_controller()
    from scout_testkit import ZB

    booking(ctrl, ZB)
    motion(ctrl, "office")
    hass.states.set(E["weather"], "cloudy", {"temperature": 6.0})
    hass.states.set("climate.office", "heat", {"current_temperature": 18.0})
    run(ctrl.async_reconcile())
    assert ctrl.applied[ZB] == PRESET_COMFORT
    assert _pushed(hass, _comfort_number("climate.office")) > ctrl.number("office_comfort_temp") - 0.01
