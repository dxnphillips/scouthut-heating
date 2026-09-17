"""Diagnostics export hygiene (2026-09-16 audit / Rointe review rec 9).

Booking titles carry hirer names and are redacted to the eco-keyword match in
the export (the in-memory audit keeps them, the download does not); the alarm
panels' states are exported; the shared zone reports a true average plus its
coldest probe; a boost press / expiry / cancel is a first-class audit event.
"""

from datetime import timedelta

from scout_testkit import E, ZA, ZB, booking, make_controller, motion, run

HB, HF = "climate.hall_back", "climate.hall_front"


def _export_events(ctrl, name):
    return [e for e in ctrl.diagnostics_data()["events"] if e.get("event") == name]


def _audit_events(ctrl, name):
    return [e for e in ctrl.audit.to_list() if e.get("event") == name]


def test_booking_titles_are_redacted_in_the_export_but_kept_in_memory():
    ctrl, hass = make_controller()
    hass.states.set(HB, "heat", {"current_temperature": 18.0})
    run(ctrl.async_reconcile())  # baseline observed: calendar off
    booking(ctrl, ZA, title="1st Pelsall Squirrels (J. Bloggs)")
    motion(ctrl, "hall")
    run(ctrl.async_reconcile())
    raw = _audit_events(ctrl, "booking_start")
    assert raw and raw[-1]["title"] == "1st pelsall squirrels (j. bloggs)"
    exported = _export_events(ctrl, "booking_start")
    assert exported and exported[-1]["title"] == "redacted"
    assert "bloggs" not in str(ctrl.diagnostics_data())
    assert ctrl.diagnostics_data()["state"]["cal_title"][ZA] == "redacted"


def test_eco_keyword_bookings_keep_only_the_matched_keyword():
    ctrl, hass = make_controller()
    hass.states.set(HB, "heat", {"current_temperature": 12.0})
    run(ctrl.async_reconcile())  # baseline observed: calendar off
    booking(ctrl, ZA, title="Sal-Vation cleaning (Mrs X)")
    run(ctrl.async_reconcile())
    exported = _export_events(ctrl, "booking_start")
    assert exported and exported[-1]["title"] == "eco:sal-vation"
    assert exported[-1]["eco"] is True
    assert "mrs x" not in str(ctrl.diagnostics_data()).lower()


def test_empty_titles_pass_through():
    ctrl, _ = make_controller()
    assert ctrl.diagnostics_data()["state"]["cal_title"] == {ZA: "", ZB: ""}


def test_alarm_panel_states_are_exported():
    ctrl, hass = make_controller()
    hass.states.set(E["alarm_main"], "armed_away")
    hass.states.set(E["alarm_office"], "disarmed")
    alarms = ctrl.diagnostics_data()["state"]["alarms"]
    assert alarms == {"main": "armed_away", "office": "disarmed"}


def test_shared_zone_exports_a_true_average_and_its_coldest():
    ctrl, hass = make_controller()
    for eid, t in zip(E["shared"], (18.0, 20.0, 22.0)):
        hass.states.set(eid, "heat", {"current_temperature": t})
    shared = ctrl.diagnostics_data()["readings"]["zones"]["shared"]
    assert shared["average"] == 20.0
    assert shared["coldest"] == 18.0


def test_boost_press_expiry_and_cancel_are_audited():
    ctrl, hass = make_controller()
    hass.states.set(HB, "heat", {"current_temperature": 17.0})
    run(ctrl.async_boost(ZA))
    pressed = _audit_events(ctrl, "boost")
    assert pressed and pressed[-1]["zone"] == ZA and pressed[-1]["minutes"] == ctrl.boost_minutes()
    assert pressed[-1]["target"] == ctrl.number("hall_comfort_temp") + ctrl.number("boost_offset")
    ctrl.boost_until[ZA] = ctrl._now() - timedelta(seconds=1)
    run(ctrl.async_reconcile())
    assert _audit_events(ctrl, "boost_expired")[-1]["zone"] == ZA
    assert ctrl.boost_until[ZA] is None
    run(ctrl.async_boost(ZA))
    run(ctrl.async_cancel_boost(ZA))
    assert _audit_events(ctrl, "boost_cancelled")[-1]["zone"] == ZA
