"""A heater back from silence is sent our settings again (v1.51.2).

Field 2026-11-05: hall_left had been dark for 41.6 h. At 11:41:28Z it uploaded
a setpoint we had not sent, and the drive's read-back flagged it that tick, but
nothing re-sent ours: `_drive_push` writes only a changed value and the hall's
preset had not changed. It ran on its own setpoint until the hall next changed
preset at 12:48Z. Earlier the same morning its frozen 17.0 had also propped up
the drive's sanity median, so two honest cold probes were called insane.
"""

from datetime import timedelta

import pytest

from custom_components.scout_hut_heating.const import CONF_HALL_CLIMATES
from custom_components.scout_hut_heating.coordinator import HEATER_SILENT_MINUTES
from scout_testkit import (
    PRESET_COMFORT,
    ZA,
    make_controller,
    naive_now,
    preheat_window,
    run,
    service_calls,
    set_registry,
    set_sync_clock,
    trust_sync_clock,
)

HB, HF = "climate.hall_back", "climate.hall_front"
NUM_HB = "number.hall_back_comfort_temperature"
NUM_HF = "number.hall_front_comfort_temperature"


def _events(ctrl, name):
    return [e for e in ctrl.audit.to_list() if e.get("event") == name]


def _hall(back=17.0, front=17.0, dark_min=HEATER_SILENT_MINUTES + 1):
    ctrl, hass = make_controller()
    set_registry(
        entries_by_device={"dhb": [HB, NUM_HB], "dhf": [HF, NUM_HF]},
        entity_devices={HB: "dhb", HF: "dhf"},
    )
    hass.states.set(NUM_HB, "19.0")
    hass.states.set(NUM_HF, "19.0")
    preheat_window(ctrl, ZA)
    hass.states.set(HB, "heat", {"current_temperature": back, "temperature": 19.0})
    hass.states.set(HF, "heat", {"current_temperature": front, "temperature": 19.0})
    devices = set_sync_clock(hass, {HB: naive_now(ctrl) - timedelta(minutes=dark_min)})
    trust_sync_clock(ctrl)
    return ctrl, hass, devices


def test_a_heard_heater_gets_the_preset_and_the_drive_value_again():
    ctrl, hass, devices = _hall()
    run(ctrl.async_reconcile())
    assert _events(ctrl, "heater_silent")
    assert ctrl.applied[ZA] == PRESET_COMFORT
    pushed = ctrl._drive_pushed[HB]
    # Nothing changes on our side, so an ordinary tick writes nothing to it.
    hass.services.calls.clear()
    run(ctrl.async_reconcile())
    assert not [
        c for c in service_calls(hass, "climate", "set_temperature")
        if c["data"]["entity_id"] == HB
    ]
    # It uploads again, on a setpoint of its own.
    hass.states.set(HB, "heat", {"current_temperature": 12.0, "temperature": 16.0})
    devices[HB].last_sync_datetime_device = naive_now(ctrl)
    run(ctrl.async_reconcile())
    (heard,) = _events(ctrl, "heater_heard")
    assert heard["resent"] == [ZA]
    presets = service_calls(hass, "climate", "set_preset_mode")
    assert any(HB in c["data"]["entity_id"] for c in presets)
    landed = [
        c["data"]["temperature"]
        for c in service_calls(hass, "climate", "set_temperature")
        if c["data"]["entity_id"] == HB
    ]
    assert landed and landed[-1] == pytest.approx(ctrl._drive_pushed[HB])
    assert ctrl._drive_pushed[HB] >= pushed - 0.5
    assert HB not in ctrl._drive_rejected  # a fresh push: the read-back waits again


def test_the_resend_is_once_not_every_tick():
    ctrl, hass, devices = _hall()
    run(ctrl.async_reconcile())
    devices[HB].last_sync_datetime_device = naive_now(ctrl)
    run(ctrl.async_reconcile())
    hass.services.calls.clear()
    run(ctrl.async_reconcile())
    assert not service_calls(hass, "climate", "set_preset_mode")


def test_a_dark_heater_does_not_prop_up_the_sanity_median():
    # Three live probes at 11, 11 and 15.5 and a dark one frozen at 17.0: the
    # 11s are the room, not insane.
    hall = {
        "climate.hall_front": 11.0,
        "climate.hall_right": 11.0,
        "climate.hall_back": 15.5,
        "climate.hall_left": 17.0,
    }
    ctrl, hass = make_controller({CONF_HALL_CLIMATES: list(hall)})
    numbers = {c: c.replace("climate.", "number.") + "_comfort_temperature" for c in hall}
    set_registry(
        entries_by_device={f"d_{c}": [c, numbers[c]] for c in hall},
        entity_devices={c: f"d_{c}" for c in hall},
    )
    for number in numbers.values():
        hass.states.set(number, "19.0")
    for climate, temp in hall.items():
        hass.states.set(climate, "heat", {"current_temperature": temp, "temperature": 19.0})
    now = naive_now(ctrl)
    set_sync_clock(
        hass,
        {
            c: now - timedelta(minutes=25 * 60 if c == "climate.hall_left" else 5)
            for c in hall
        },
    )
    trust_sync_clock(ctrl)
    preheat_window(ctrl, ZA)
    run(ctrl.async_reconcile())
    assert ctrl.applied[ZA] == PRESET_COMFORT
    # Driven, not withdrawn (an insane withdrawal leaves the driven set).
    assert {"climate.hall_front", "climate.hall_right"} <= ctrl._drive_driven
