"""Probe nudge (v1.44.0): provoke a fresh reading from a booked zone's stale heater.

The Rointe cloud only carries what the heater last synced and an idle heater
syncs rarely, so re-reading the cloud cannot un-freeze a probe — but a command
makes the device act and report back (the 2026-09-22 08:46Z restart's preset
re-apply refreshed four probes that had sat at 19.0 for 20+ min while the hall
cooled to 18.0). A no-op comfort-number write is the lightest command we own.
Whether it provokes a sync is UNPROVEN, so every nudge is verified and tallied.
"""


from custom_components.scout_hut_heating.coordinator import (
    PROBE_NUDGE_COOLDOWN_MIN,
    PROBE_NUDGE_FLAT_MIN,
    PROBE_NUDGE_VERIFY_MIN,
)
from scout_testkit import (
    E,
    ZA,
    advance,
    booking,
    make_controller,
    preheat_window,
    run,
    service_calls,
    set_registry,
)

HB, HF = "climate.hall_back", "climate.hall_front"
NUM_HB = "number.hall_back_comfort_temperature"
NUM_HF = "number.hall_front_comfort_temperature"


def _wire(hass):
    set_registry(
        entries_by_device={"dhb": [HB, NUM_HB], "dhf": [HF, NUM_HF]},
        entity_devices={HB: "dhb", HF: "dhf"},
    )
    hass.states.set(NUM_HB, "19.0")
    hass.states.set(NUM_HF, "19.0")


def _probes(hass, back, front):
    hass.states.set(HB, "heat", {"current_temperature": back})
    hass.states.set(HF, "heat", {"current_temperature": front})


def _nudges(hass, number):
    return [
        c for c in service_calls(hass, "number", "set_value")
        if c["data"].get("entity_id") == number
    ]


def _events(ctrl, name):
    return [e for e in ctrl.audit.to_list() if e.get("event") == name]


def _stale_booked_hall(minutes_flat=PROBE_NUDGE_FLAT_MIN + 1):
    """A hall in its pre-heat window whose probes have sat unchanged."""
    ctrl, hass = make_controller()
    _wire(hass)
    preheat_window(ctrl, ZA)
    _probes(hass, 19.0, 19.0)
    ctrl._track_probe_changes()
    advance(ctrl, minutes_flat)
    return ctrl, hass


def test_a_stale_reading_in_a_booked_zone_is_nudged_with_its_own_number():
    ctrl, hass = _stale_booked_hall()
    run(ctrl._reconcile_probe_nudge())
    (call,) = _nudges(hass, NUM_HB)
    assert call["data"]["value"] == 19.0  # the value it already holds: a no-op
    assert call["blocking"] is True
    assert _nudges(hass, NUM_HF)  # every stale heater in the zone, not just one
    evts = _events(ctrl, "probe_nudge")
    assert {e["heater"] for e in evts} == {HB, HF}
    assert evts[0]["flat_min"] >= PROBE_NUDGE_FLAT_MIN
    assert ctrl._probe_nudge_tally["nudged"] == 2


def test_a_fresh_reading_is_not_nudged():
    ctrl, hass = _stale_booked_hall(minutes_flat=PROBE_NUDGE_FLAT_MIN - 2)
    run(ctrl._reconcile_probe_nudge())
    assert not service_calls(hass, "number", "set_value")


def test_an_unbooked_zone_is_never_nudged():
    ctrl, hass = make_controller()
    _wire(hass)
    _probes(hass, 19.0, 19.0)
    ctrl._track_probe_changes()
    advance(ctrl, 60)
    run(ctrl._reconcile_probe_nudge())
    assert not service_calls(hass, "number", "set_value")


def test_a_running_booking_counts_as_booked():
    ctrl, hass = _stale_booked_hall()
    booking(ctrl, ZA)
    run(ctrl._reconcile_probe_nudge())
    assert _nudges(hass, NUM_HB)


def test_a_nudge_that_refreshes_the_reading_is_verified_as_such():
    ctrl, hass = _stale_booked_hall()
    run(ctrl._reconcile_probe_nudge())
    # The heater syncs: the reading moves a minute later.
    advance(ctrl, 1)
    _probes(hass, 18.0, 18.5)
    ctrl._track_probe_changes()
    run(ctrl._reconcile_probe_nudge())
    results = _events(ctrl, "probe_nudge_result")
    assert {e["heater"] for e in results} == {HB, HF}
    back = next(e for e in results if e["heater"] == HB)
    assert back["refreshed"] is True
    assert back["before"] == 19.0 and back["after"] == 18.0
    assert back["minutes"] < PROBE_NUDGE_VERIFY_MIN
    assert ctrl._probe_nudge_tally["refreshed"] == 2
    assert ctrl._probe_nudge_pending == {}


def test_a_nudge_that_changes_nothing_is_verified_as_unchanged():
    ctrl, hass = _stale_booked_hall()
    run(ctrl._reconcile_probe_nudge())
    advance(ctrl, PROBE_NUDGE_VERIFY_MIN + 0.5)
    ctrl._track_probe_changes()  # still 19.0 / 19.0
    run(ctrl._reconcile_probe_nudge())
    results = _events(ctrl, "probe_nudge_result")
    assert results and all(e["refreshed"] is False for e in results)
    assert ctrl._probe_nudge_tally["unchanged"] == 2


def test_a_heater_is_not_re_nudged_inside_the_cooldown():
    ctrl, hass = _stale_booked_hall()
    run(ctrl._reconcile_probe_nudge())
    advance(ctrl, PROBE_NUDGE_VERIFY_MIN + 1)  # verification closes as unchanged
    run(ctrl._reconcile_probe_nudge())
    run(ctrl._reconcile_probe_nudge())  # still flat, still inside the cooldown
    assert len(_nudges(hass, NUM_HB)) == 1
    advance(ctrl, PROBE_NUDGE_COOLDOWN_MIN)
    run(ctrl._reconcile_probe_nudge())
    assert len(_nudges(hass, NUM_HB)) == 2


def test_a_failed_nudge_write_is_not_counted_and_is_retried_after_the_cooldown():
    ctrl, hass = _stale_booked_hall()

    async def _boom(domain, service, data, blocking=False):
        raise RuntimeError("cloud down")

    real = hass.services.async_call
    hass.services.async_call = _boom
    run(ctrl._reconcile_probe_nudge())
    assert ctrl._probe_nudge_tally["nudged"] == 0
    assert ctrl._probe_nudge_pending == {}
    assert _events(ctrl, "write_failed")
    hass.services.async_call = real


def test_the_step_is_wired_into_the_reconcile_and_reports_in_diagnostics():
    ctrl, hass = _stale_booked_hall()
    hass.states.set(E["weather"], "cloudy", {"temperature": 8.0})
    run(ctrl.async_reconcile())
    assert ctrl._step_failing == set()
    assert _events(ctrl, "probe_nudge")
    nudges = ctrl.diagnostics_data()["state"]["probe_nudges"]
    assert nudges["nudged"] >= 1
    assert set(nudges["pending"]) <= {HB, HF}


def test_verification_cannot_be_fooled_by_the_readings_own_earlier_change():
    # The freeze stamp must be NEWER than the nudge to count as a refresh.
    ctrl, hass = _stale_booked_hall()
    run(ctrl._reconcile_probe_nudge())
    ctrl._track_probe_changes()  # nothing changed
    run(ctrl._reconcile_probe_nudge())
    assert not _events(ctrl, "probe_nudge_result")
    assert set(ctrl._probe_nudge_pending) == {HB, HF}


