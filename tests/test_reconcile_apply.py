"""End-to-end reconcile: assert the actual service calls / applied state."""

from scout_testkit import (
    PRESET_COMFORT,
    PRESET_ICE,
    ZA,
    booking,
    make_controller,
    motion,
    run,
    service_calls,
    preset_for,
    set_registry,
)


def test_empty_reconcile_ices_all_and_off_water():
    ctrl, hass = make_controller()
    run(ctrl.async_reconcile())
    assert ctrl.applied[ZA] == PRESET_ICE
    assert ctrl.applied["shared"] == PRESET_ICE
    assert preset_for(hass, "climate.hall_back") == PRESET_ICE
    assert preset_for(hass, "climate.office") == PRESET_ICE
    assert preset_for(hass, "climate.kitchen") == PRESET_ICE
    assert ctrl.water_on is False
    assert service_calls(hass, "switch", "turn_off")


def test_water_turns_on_with_kitchen_motion():
    ctrl, hass = make_controller()
    motion(ctrl, "kitchen")
    run(ctrl.async_reconcile())
    assert ctrl.water_on is True
    assert service_calls(hass, "switch", "turn_on")


def test_booking_pushes_hall_setpoints():
    set_registry(
        entries_by_device={
            "d1": ["climate.hall_back", "number.hall_back_comfort_temperature", "number.hall_back_eco_temperature"],
            "d2": ["climate.hall_front", "number.hall_front_comfort_temperature", "number.hall_front_eco_temperature"],
        },
        entity_devices={"climate.hall_back": "d1", "climate.hall_front": "d2"},
    )
    ctrl, hass = make_controller()
    booking(ctrl, ZA)
    motion(ctrl, "hall")
    run(ctrl.async_reconcile())
    assert ctrl.applied[ZA] == PRESET_COMFORT
    assert service_calls(hass, "number", "set_value")


def test_reconcile_is_idempotent():
    ctrl, hass = make_controller()
    run(ctrl.async_reconcile())
    first = len(service_calls(hass, "climate", "set_preset_mode"))
    run(ctrl.async_reconcile())  # nothing changed
    second = len(service_calls(hass, "climate", "set_preset_mode"))
    assert first == second


def test_preset_reapplied_when_state_changes():
    ctrl, hass = make_controller()
    run(ctrl.async_reconcile())  # -> ice
    booking(ctrl, ZA)
    motion(ctrl, "hall")
    run(ctrl.async_reconcile())  # -> comfort
    assert preset_for(hass, "climate.hall_back") == PRESET_COMFORT
