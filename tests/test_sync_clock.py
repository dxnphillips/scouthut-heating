"""The device sync clock (v1.45.0): stale versus static, settled by the heater.

The Rointe cloud only carries what the heater last uploaded, and the Rointe
integration's 15-second poll keeps HA current with the cloud, not the room, so
no HA state can tell a SILENT heater from a STATIC one. The heater's own
`last_sync_datetime_device` can. The SDK parses it into every device object but
publishes it nowhere, so it is read straight from that integration's in-memory
objects — read-only, guarded, and trusted only once a stamp has been seen
standing still (the SDK's missing-key fallback is the current time every poll).
"""

from datetime import timedelta
from types import SimpleNamespace

from custom_components.scout_hut_heating.coordinator import (
    BOOKING_RELIGHT_STALE_MIN,
    PRESET_ICE,
    PROBE_NUDGE_COOLDOWN_MIN,
    PROBE_NUDGE_FLAT_MIN,
    PROBE_NUDGE_VERIFY_MIN,
    SYNC_CLOCK_TRUST_MIN,
)
from scout_testkit import (
    ZA,
    advance,
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


def _wire(hass):
    set_registry(
        entries_by_device={"dhb": [HB, NUM_HB], "dhf": [HF, NUM_HF]},
        entity_devices={HB: "dhb", HF: "dhf"},
    )
    hass.states.set(NUM_HB, "19.0")
    hass.states.set(NUM_HF, "19.0")


def _probes(hass, back, front, setpoint=19.0):
    hass.states.set(HB, "heat", {"current_temperature": back, "temperature": setpoint})
    hass.states.set(HF, "heat", {"current_temperature": front, "temperature": setpoint})


def _events(ctrl, name):
    return [e for e in ctrl.audit.to_list() if e.get("event") == name]


def _calls(hass, domain, service, entity):
    return [
        c for c in service_calls(hass, domain, service)
        if c["data"].get("entity_id") == entity
    ]


def _trusted_hall(minutes_old=40.0):
    """A booked hall whose hall_back has a TRUSTED clock; hall_front has none."""
    ctrl, hass = make_controller()
    _wire(hass)
    preheat_window(ctrl, ZA)
    _probes(hass, 19.0, 19.0)
    devices = set_sync_clock(
        hass, {HB: naive_now(ctrl) - timedelta(minutes=minutes_old)}
    )
    trust_sync_clock(ctrl)
    assert ctrl._sync_clock_trusted(HB)
    return ctrl, hass, devices


# --- reading the stamp -------------------------------------------------------


def test_the_stamp_is_read_through_the_registry_chain():
    ctrl, hass = make_controller()
    _wire(hass)
    stamp = naive_now(ctrl) - timedelta(minutes=5)
    set_sync_clock(hass, {HB: stamp})
    assert ctrl._heater_sync_stamp(HB) == stamp
    assert ctrl._heater_sync_stamp(HF) is None  # no Rointe device behind it


def test_every_missing_link_degrades_to_none_and_never_raises():
    ctrl, hass = make_controller()
    _wire(hass)
    # No Rointe integration loaded at all.
    assert ctrl._heater_sync_stamp(HB) is None
    ctrl._track_probe_changes()
    assert ctrl._sync_clock_available is False
    assert not _events(ctrl, "sync_clock_lost")  # never had it: nothing to lose
    # A device object whose attribute was renamed by an upgrade.
    devices = set_sync_clock(hass, {HB: naive_now(ctrl)})
    devices[HB] = SimpleNamespace(something_else=1)
    hass.data["rointe"]["entry"].device_manager.rointe_devices["rointe-" + HB] = devices[HB]
    assert ctrl._heater_sync_stamp(HB) is None
    # A coordinator object of an unexpected shape.
    hass.data["rointe"] = {"entry": object()}
    assert ctrl._heater_sync_stamp(HB) is None
    ctrl._track_probe_changes()
    assert ctrl._sync_clock_available is False


def test_the_clock_appearing_and_vanishing_is_audited_on_the_edges():
    ctrl, hass = make_controller()
    _wire(hass)
    set_sync_clock(hass, {HB: naive_now(ctrl)})
    ctrl._track_probe_changes()
    assert ctrl._sync_clock_available is True
    assert len(_events(ctrl, "sync_clock_found")) == 1
    ctrl._track_probe_changes()
    assert len(_events(ctrl, "sync_clock_found")) == 1  # edge, not every tick
    hass.data.pop("rointe")
    ctrl._track_probe_changes()
    assert len(_events(ctrl, "sync_clock_lost")) == 1
    assert not ctrl._sync_trusted


# --- trust -------------------------------------------------------------------


def test_a_stamp_is_trusted_only_once_it_has_stood_still():
    ctrl, hass = make_controller()
    _wire(hass)
    set_sync_clock(hass, {HB: naive_now(ctrl) - timedelta(minutes=30)})
    ctrl._track_probe_changes()
    assert not ctrl._sync_clock_trusted(HB)
    assert ctrl._sync_age_minutes(HB) is None  # untrusted: no age offered
    advance(ctrl, SYNC_CLOCK_TRUST_MIN + 0.5)
    ctrl._track_probe_changes()
    assert ctrl._sync_clock_trusted(HB)
    (evt,) = _events(ctrl, "sync_clock_trusted")
    assert evt["heater"] == HB
    assert evt["skew_min"] is not None


def test_the_sdks_missing_key_fallback_is_never_trusted():
    # When the payload lacks the key the SDK substitutes datetime.now() on
    # every poll — a stamp that never stands still. It must never gate anything.
    ctrl, hass = make_controller()
    _wire(hass)
    _probes(hass, 19.0, 19.0)
    devices = set_sync_clock(hass, {HB: naive_now(ctrl)})
    for _ in range(5):
        ctrl._track_probe_changes()
        advance(ctrl, SYNC_CLOCK_TRUST_MIN + 1)
        devices[HB].last_sync_datetime_device = naive_now(ctrl)  # "fresh" again
    ctrl._track_probe_changes()
    assert not ctrl._sync_clock_trusted(HB)
    assert abs(ctrl._probe_silent_minutes(HB) - ctrl._probe_flat_minutes(HB)) < 0.01


# --- what the age is ---------------------------------------------------------


def test_age_is_how_long_we_watched_the_stamp_floored_by_the_devices_own_reckoning():
    ctrl, hass, devices = _trusted_hall(minutes_old=40)
    # Trusted after 2.5 min of watching, but the device says 40: the floor wins.
    assert ctrl._sync_age_minutes(HB) >= 40
    # A fresh upload: the age collapses to the device's ~1 min.
    devices[HB].last_sync_datetime_device = naive_now(ctrl) - timedelta(minutes=1)
    ctrl._track_probe_changes()
    assert 0.9 <= ctrl._sync_age_minutes(HB) <= 1.5
    # Then time passes with no upload: our watch carries the age.
    advance(ctrl, 25)
    assert 25 <= ctrl._sync_age_minutes(HB) <= 27


def test_a_stamp_in_the_future_is_judged_on_our_watch_alone():
    ctrl, hass, devices = _trusted_hall()
    devices[HB].last_sync_datetime_device = naive_now(ctrl) + timedelta(hours=1)
    ctrl._track_probe_changes()
    assert ctrl._sync_age_minutes(HB) < 0.5
    advance(ctrl, 10)
    assert 10 <= ctrl._sync_age_minutes(HB) <= 11


# --- silent versus static ----------------------------------------------------


def test_a_flat_reading_that_the_heater_keeps_uploading_is_static_not_stale():
    # Field 2026-09-22: hall probes sat on 18.5 while the room was genuinely
    # static. With the clock, a value flat for half an hour but uploaded a
    # minute ago is fresh — and hall_front, with no clock, still reads flat.
    ctrl, hass, devices = _trusted_hall()
    advance(ctrl, 30)
    devices[HB].last_sync_datetime_device = naive_now(ctrl) - timedelta(minutes=1)
    ctrl._track_probe_changes()
    assert ctrl._probe_flat_minutes(HB) > 30
    assert ctrl._probe_silent_minutes(HB) < 2
    assert not ctrl._probe_frozen(HB, 20)
    assert abs(ctrl._probe_silent_minutes(HF) - ctrl._probe_flat_minutes(HF)) < 0.01
    assert ctrl._probe_frozen(HF, 20)


def test_a_silent_heater_is_stale_however_plausible_its_value():
    ctrl, hass, devices = _trusted_hall(minutes_old=1)
    advance(ctrl, 30)
    ctrl._track_probe_changes()
    assert ctrl._probe_silent_minutes(HB) >= 30
    assert ctrl._probe_frozen(HB, 20)


def test_the_stale_relight_fires_on_silence_not_on_a_flat_value():
    ctrl, hass, devices = _trusted_hall()
    # hall_back is the coldest probe; iced as warm-enough in the window.
    _probes(hass, 19.0, 19.5)
    ctrl._track_probe_changes()
    ctrl.applied[ZA] = PRESET_ICE
    ctrl._preset_reason[ZA] = "booking_warm"
    advance(ctrl, BOOKING_RELIGHT_STALE_MIN + 1)
    # Flat for 21 min — but the heater uploaded a minute ago: genuinely 19.0.
    devices[HB].last_sync_datetime_device = naive_now(ctrl) - timedelta(minutes=1)
    ctrl._track_probe_changes()
    assert not ctrl._iced_on_a_stale_reading(ZA, "booking_warm")
    # Now silent for 21 min: relit.
    advance(ctrl, BOOKING_RELIGHT_STALE_MIN + 1)
    assert ctrl._iced_on_a_stale_reading(ZA, "booking_warm")


# --- the nudge on the clock --------------------------------------------------


def test_a_static_but_uploading_heater_is_not_nudged():
    ctrl, hass, devices = _trusted_hall()
    advance(ctrl, PROBE_NUDGE_FLAT_MIN + 1)
    devices[HB].last_sync_datetime_device = naive_now(ctrl) - timedelta(minutes=1)
    ctrl._track_probe_changes()
    run(ctrl._reconcile_probe_nudge())
    assert not _calls(hass, "number", "set_value", NUM_HB)
    assert _calls(hass, "number", "set_value", NUM_HF)  # no clock: flat rule


def test_a_silent_heater_is_nudged_and_the_event_says_the_clock_judged_it():
    ctrl, hass, devices = _trusted_hall(minutes_old=PROBE_NUDGE_FLAT_MIN + 1)
    run(ctrl._reconcile_probe_nudge())
    assert _calls(hass, "number", "set_value", NUM_HB)
    evt = next(e for e in _events(ctrl, "probe_nudge") if e["heater"] == HB)
    assert evt["clock"] is True and evt["method"] == "number"
    assert evt["flat_min"] >= PROBE_NUDGE_FLAT_MIN


def test_an_upload_after_the_nudge_with_the_reading_unchanged_is_synced():
    ctrl, hass, devices = _trusted_hall(minutes_old=PROBE_NUDGE_FLAT_MIN + 1)
    run(ctrl._reconcile_probe_nudge())
    advance(ctrl, 1)
    devices[HB].last_sync_datetime_device = naive_now(ctrl)  # the device answered
    ctrl._track_probe_changes()  # reading still 19.0
    run(ctrl._reconcile_probe_nudge())
    back = next(e for e in _events(ctrl, "probe_nudge_result") if e["heater"] == HB)
    assert back["outcome"] == "synced"
    assert back["uploaded"] is True and back["clock"] is True
    assert back["sync_before"] != back["sync_after"]
    assert ctrl._probe_nudge_tally["synced"] == 1
    assert not ctrl._nudge_escalated


def test_no_upload_by_the_verify_window_on_a_trusted_clock_is_missed_and_escalates():
    ctrl, hass, devices = _trusted_hall(minutes_old=PROBE_NUDGE_FLAT_MIN + 1)
    advance(ctrl, PROBE_NUDGE_FLAT_MIN)  # hall_front (no clock) goes flat too
    run(ctrl._reconcile_probe_nudge())
    advance(ctrl, PROBE_NUDGE_VERIFY_MIN + 0.5)
    ctrl._track_probe_changes()  # stamp untouched, reading untouched
    run(ctrl._reconcile_probe_nudge())
    results = {e["heater"]: e for e in _events(ctrl, "probe_nudge_result")}
    assert results[HB]["outcome"] == "missed"
    assert results[HF]["outcome"] == "inconclusive"  # no clock on hall_front
    assert ctrl._probe_nudge_tally["missed"] == 1
    assert ctrl._nudge_escalated
    (esc,) = _events(ctrl, "probe_nudge_escalated")
    assert esc["heater"] == HB
    assert ctrl.diagnostics_data()["state"]["probe_nudges"]["method"] == "climate"


def test_an_escalated_nudge_re_sends_the_setpoint_the_heater_already_holds():
    ctrl, hass, devices = _trusted_hall(minutes_old=PROBE_NUDGE_FLAT_MIN + 1)
    ctrl._nudge_escalated = True
    # hall_back is being driven: the drive's own pushed value is re-sent, never
    # the (possibly lagging) live attribute. hall_front idles at its live 19.0.
    ctrl._drive_driven.add(HB)
    ctrl._drive_pushed[HB] = 20.0
    _probes(hass, 19.0, 19.0, setpoint=19.5)
    ctrl._track_probe_changes()
    advance(ctrl, PROBE_NUDGE_FLAT_MIN)  # hall_front (no clock) goes flat too
    run(ctrl._reconcile_probe_nudge())
    assert not service_calls(hass, "number", "set_value")
    (back,) = _calls(hass, "climate", "set_temperature", HB)
    assert back["data"]["temperature"] == 20.0
    (front,) = _calls(hass, "climate", "set_temperature", HF)
    assert front["data"]["temperature"] == 19.5
    evt = next(e for e in _events(ctrl, "probe_nudge") if e["heater"] == HB)
    assert evt["method"] == "climate" and evt["value"] == 20.0
    assert ctrl._probe_nudge_pending[HB]["method"] == "climate"


def test_a_missed_climate_nudge_stays_a_miss_with_nothing_heavier_to_try():
    ctrl, hass, devices = _trusted_hall(minutes_old=PROBE_NUDGE_FLAT_MIN + 1)
    ctrl._nudge_escalated = True
    run(ctrl._reconcile_probe_nudge())
    advance(ctrl, PROBE_NUDGE_VERIFY_MIN + 0.5)
    ctrl._track_probe_changes()
    run(ctrl._reconcile_probe_nudge())
    back = next(e for e in _events(ctrl, "probe_nudge_result") if e["heater"] == HB)
    assert back["outcome"] == "missed" and back["method"] == "climate"
    assert len(_events(ctrl, "probe_nudge_escalated")) == 0
    # And it is retried after the cooldown, still by the climate command.
    advance(ctrl, PROBE_NUDGE_COOLDOWN_MIN)
    run(ctrl._reconcile_probe_nudge())
    assert len(_calls(hass, "climate", "set_temperature", HB)) == 2


# --- the export --------------------------------------------------------------


def test_diagnostics_and_the_trace_carry_the_clock():
    ctrl, hass, devices = _trusted_hall(minutes_old=40)
    ctrl._track_probe_changes()
    data = ctrl.diagnostics_data()
    back = data["readings"]["zones"][ZA]["heaters"][HB]
    assert back["sync_clock"] == "trusted"
    assert back["sync_age_min"] >= 40
    assert back["sync_at"] is not None and back["sync_skew_min"] is not None
    assert "sync_clock" not in data["readings"]["zones"][ZA]["heaters"][HF]
    assert data["state"]["sync_clock"] == {"available": True, "trusted": [HB]}
    ctrl._sample_trace()
    assert ctrl.trace.to_list()[-1]["hall_sync"] >= 40
