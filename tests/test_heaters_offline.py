"""Sustained heater outage alert (Q18; cause: the Rointe Nexa token expiring).

A zone whose heaters have ALL been unreachable for HEATERS_OFFLINE_MINUTES
raises an audit event + notification; a single-poll blip does not; recovery
is audited and the notification dismissed; a zone never seen online is not
judged inside the startup grace.
"""

from datetime import timedelta

from custom_components.scout_hut_heating.coordinator import HEATERS_OFFLINE_MINUTES
from scout_testkit import E, ZA, ZB, make_controller, run

HALL = ("climate.hall_back", "climate.hall_front")


def _events(ctrl, name):
    return [e for e in ctrl.audit.to_list() if e.get("event") == name]


def _ctrl_past_grace():
    ctrl, hass = make_controller()
    ctrl._started_at = ctrl._now() - timedelta(hours=1)
    return ctrl, hass


def _hall(hass, state):
    for c in HALL:
        hass.states.set(c, state, {"current_temperature": 18.0})


def _age_offline(ctrl, zone, minutes):
    ctrl._heaters_offline_since[zone] = ctrl._now() - timedelta(minutes=minutes)


def test_sustained_all_heaters_offline_is_alerted_and_recovery_audited():
    ctrl, hass = _ctrl_past_grace()
    _hall(hass, "heat")
    run(ctrl.async_reconcile())
    _hall(hass, "unavailable")
    run(ctrl.async_reconcile())
    assert ctrl._heaters_offline_since[ZA] is not None
    assert not _events(ctrl, "heaters_offline")  # clock started, not yet an outage
    _age_offline(ctrl, ZA, HEATERS_OFFLINE_MINUTES + 1)
    run(ctrl.async_reconcile())
    outage = _events(ctrl, "heaters_offline")
    assert outage and outage[-1]["zone"] == ZA and outage[-1]["minutes"] >= HEATERS_OFFLINE_MINUTES
    assert ZA in ctrl._heaters_offline_notified
    assert ctrl.diagnostics_data()["state"]["heaters_offline"].get(ZA)
    run(ctrl.async_reconcile())
    assert len(_events(ctrl, "heaters_offline")) == 1  # once per episode
    # Recovery: the heaters come back.
    _hall(hass, "heat")
    run(ctrl.async_reconcile())
    back = _events(ctrl, "heaters_online")
    assert back and back[-1]["zone"] == ZA and back[-1]["minutes"] >= HEATERS_OFFLINE_MINUTES
    assert ZA not in ctrl._heaters_offline_notified
    assert ctrl._heaters_offline_since[ZA] is None


def test_a_single_poll_blip_is_not_an_outage():
    ctrl, hass = _ctrl_past_grace()
    _hall(hass, "heat")
    run(ctrl.async_reconcile())
    _hall(hass, "unavailable")
    run(ctrl.async_reconcile())
    _hall(hass, "heat")
    run(ctrl.async_reconcile())
    assert not _events(ctrl, "heaters_offline")
    assert not _events(ctrl, "heaters_online")  # nothing was alerted, nothing to clear


def test_one_heater_still_online_is_not_a_zone_outage():
    ctrl, hass = _ctrl_past_grace()
    _hall(hass, "heat")
    run(ctrl.async_reconcile())
    hass.states.set(HALL[0], "unavailable")
    run(ctrl.async_reconcile())
    assert ctrl._heaters_offline_since.get(ZA) is None


def test_every_zone_down_flags_the_token_signature():
    ctrl, hass = _ctrl_past_grace()
    _hall(hass, "heat")
    hass.states.set("climate.office", "heat", {"current_temperature": 18.0})
    for eid in E["shared"]:
        hass.states.set(eid, "heat", {"current_temperature": 18.0})
    run(ctrl.async_reconcile())
    _hall(hass, "unavailable")
    hass.states.set("climate.office", "unavailable")
    for eid in E["shared"]:
        hass.states.set(eid, "unavailable")
    run(ctrl.async_reconcile())
    for zone in (ZA, ZB, "shared"):
        _age_offline(ctrl, zone, HEATERS_OFFLINE_MINUTES + 1)
    run(ctrl.async_reconcile())
    outages = _events(ctrl, "heaters_offline")
    assert {e["zone"] for e in outages} == {ZA, ZB, "shared"}
    assert all(e["all_zones"] is True for e in outages)


def test_never_seen_online_is_not_judged_inside_the_startup_grace():
    ctrl, hass = make_controller()  # freshly started; heaters have no state yet
    ctrl._started_at = ctrl._now()
    run(ctrl.async_reconcile())
    assert ctrl._heaters_offline_since.get(ZA) is None
    # Past the grace the clock does start, even for a zone never seen online
    # (a boot straight into an outage still gets surfaced, just later).
    ctrl._started_at = ctrl._now() - timedelta(hours=1)
    run(ctrl.async_reconcile())
    assert ctrl._heaters_offline_since.get(ZA) is not None
