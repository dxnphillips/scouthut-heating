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
    HEATER_SILENT_MINUTES,
    PRESET_ICE,
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
    # No skew yet: the first sighting only says how old the stamp already was.
    assert evt.get("skew_min") is None


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
    # A fresh upload: the age collapses to the device's ~1 min, and the skew
    # (device clock against ours) is measured on this live upload, not before.
    assert ctrl._sync_skew.get(HB) is None
    devices[HB].last_sync_datetime_device = naive_now(ctrl) - timedelta(minutes=1)
    ctrl._track_probe_changes()
    assert 0.9 <= ctrl._sync_age_minutes(HB) <= 1.5
    assert 0.9 <= ctrl._sync_skew[HB] <= 1.5
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
    assert not ctrl._iced_on_a_stale_reading(ZA, "booking_warm", 19.0)
    # Now silent for 21 min: relit.
    advance(ctrl, BOOKING_RELIGHT_STALE_MIN + 1)
    assert ctrl._iced_on_a_stale_reading(ZA, "booking_warm", 19.0)


def test_a_held_stale_relight_releases_on_a_same_value_upload():
    # Relit on a silent heater; the heater then uploads the SAME value. On a
    # trusted clock that upload is a fresh reading: the hold ends and the
    # ordinary gate takes over (here still heating — 19.0 is inside the
    # release band — but by the ordinary reason, not the stale one).
    ctrl, hass, devices = _trusted_hall(minutes_old=BOOKING_RELIGHT_STALE_MIN + 5)
    advance(ctrl, BOOKING_RELIGHT_STALE_MIN + 5)  # hall_front (no clock) flat too
    ctrl._numbers["hall_comfort_temp"].native_value = 19.0
    ctrl.applied[ZA] = PRESET_ICE
    ctrl._preset_reason[ZA] = "booking_warm"
    assert ctrl._desired_zone(ZA) == "comfort"
    assert ctrl._preset_reason[ZA] == "preheat_stale"
    ctrl.applied[ZA] = "comfort"
    ctrl._track_probe_changes()
    assert ctrl._desired_zone(ZA) == "comfort"
    assert ctrl._preset_reason[ZA] == "preheat_stale"  # still silent: held
    devices[HB].last_sync_datetime_device = naive_now(ctrl)  # uploaded, same 19.0
    ctrl._track_probe_changes()
    assert ctrl._desired_zone(ZA) == "comfort"
    assert ctrl._preset_reason[ZA] == "preheat"  # released to the ordinary gate


# --- the heartbeat --------------------------------------------------------------


def test_the_stale_relight_line_sits_above_the_measured_idle_heartbeat():
    # 2026-09-26/27, the first night on the clock: an idle Rointe uploads about
    # every 30 min whether or not its reading changed (`hall_sync` cycled 8–29
    # min through hour-long flat readings). A heater 30 min into that ordinary
    # gap is quiet, not stale — not relit (v1.45.3; the old 20-min line sat
    # inside the cadence). Past the line it is.
    assert BOOKING_RELIGHT_STALE_MIN > 30
    for silent, expected in ((30, False), (BOOKING_RELIGHT_STALE_MIN + 1, True)):
        ctrl, hass, devices = _trusted_hall(minutes_old=silent)
        advance(ctrl, silent)  # hall_front (no clock) goes flat for as long
        ctrl.applied[ZA] = PRESET_ICE
        ctrl._preset_reason[ZA] = "booking_warm"
        assert ctrl._iced_on_a_stale_reading(ZA, "booking_warm", 19.0) is expected


def test_nothing_writes_to_a_silent_heater_just_to_wake_it():
    # v1.46.0: the probe nudge is gone. 102 no-op writes on 2026-09-27 showed a
    # Rointe uploads on its own heartbeat, on a reading change and on a real
    # setpoint change — never on a write of the value it already holds.
    ctrl, hass, devices = _trusted_hall(minutes_old=90)
    run(ctrl.async_reconcile())
    assert not hasattr(ctrl, "_reconcile_probe_nudge")
    assert not [e for e in ctrl.audit.to_list() if e.get("event", "").startswith("probe_nudge")]
    assert "probe_nudges" not in ctrl.diagnostics_data()["state"]
    assert ctrl.diagnostics_data()["state"]["reconcile_failing"] == []


# --- the export --------------------------------------------------------------


def test_diagnostics_and_the_trace_carry_the_clock():
    ctrl, hass, devices = _trusted_hall(minutes_old=40)
    ctrl._track_probe_changes()
    data = ctrl.diagnostics_data()
    back = data["readings"]["zones"][ZA]["heaters"][HB]
    assert back["sync_clock"] == "trusted"
    assert back["sync_age_min"] >= 40
    assert back["sync_at"] is not None and back["sync_skew_min"] is None
    assert "sync_clock" not in data["readings"]["zones"][ZA]["heaters"][HF]
    assert data["state"]["sync_clock"] == {"available": True, "trusted": [HB]}
    ctrl._sample_trace()
    assert ctrl.trace.to_list()[-1]["hall_sync"] >= 40


# --- one dark heater (v1.50.1) -------------------------------------------------


def test_a_heater_inside_the_line_is_quiet_not_silent():
    ctrl, hass, devices = _trusted_hall(minutes_old=HEATER_SILENT_MINUTES - 30)
    hass.services.register("notify", "mobile_app_phone")
    run(ctrl.async_reconcile())
    assert not _events(ctrl, "heater_silent")
    assert not [c for c in hass.services.calls if c["service"] == "mobile_app_phone"]


def test_a_heater_silent_past_the_line_is_alerted_once_and_heard_again():
    # 2026-10-13: hall_left stopped uploading at 18:02Z and stayed dark for 25 h.
    # HA showed it online, its reading froze at 18.0 and the cloud echoed every
    # setpoint we wrote, so nothing but the sync clock could tell. (The age is
    # floored by the device's own reckoning, which the testkit's clock rewind
    # does not move, so the dark heater is built past the line.)
    ctrl, hass, devices = _trusted_hall(minutes_old=HEATER_SILENT_MINUTES + 1)
    hass.services.register("notify", "mobile_app_phone")
    run(ctrl.async_reconcile())
    (evt,) = _events(ctrl, "heater_silent")
    assert evt["heater"] == HB and evt["zone"] == ZA
    assert evt["minutes"] >= HEATER_SILENT_MINUTES
    assert evt["reading"] == 19.0 and evt["sync_at"] is not None
    assert HB in ctrl.diagnostics_data()["state"]["heaters_silent"]
    pushes = [c for c in hass.services.calls if c["service"] == "mobile_app_phone"]
    assert len(pushes) == 1 and "not reporting" in pushes[0]["data"]["message"]
    run(ctrl.async_reconcile())
    assert len(_events(ctrl, "heater_silent")) == 1  # once per episode
    assert len([c for c in hass.services.calls if c["service"] == "mobile_app_phone"]) == 1
    # It uploads again (the same value): heard, cleared.
    devices[HB].last_sync_datetime_device = naive_now(ctrl)
    run(ctrl.async_reconcile())
    (back,) = _events(ctrl, "heater_heard")
    assert back["heater"] == HB and back["minutes"] >= HEATER_SILENT_MINUTES
    assert ctrl.diagnostics_data()["state"]["heaters_silent"] == {}


def test_a_heater_without_a_clock_is_never_called_silent():
    # hall_front has no Rointe device behind it: its age is unknowable, and an
    # unknowable age is not an alarm.
    ctrl, hass, devices = _trusted_hall(minutes_old=1)
    advance(ctrl, HEATER_SILENT_MINUTES * 3)
    ctrl._track_probe_changes()
    run(ctrl.async_reconcile())
    assert [e["heater"] for e in _events(ctrl, "heater_silent")] == [HB]


def test_the_silence_line_sits_well_clear_of_the_idle_heartbeat():
    # An idle Rointe uploads about every 30 min with one 60-min gap seen
    # (2026-09-27); the line is several missed beats, never a quiet one.
    assert HEATER_SILENT_MINUTES >= 120
