"""Sustained heater outage alert (Q18; cause: the Rointe Nexa token expiring).

A zone whose heaters have ALL been unreachable for HEATERS_OFFLINE_MINUTES
raises an audit event + notification; a single-poll blip does not; recovery
is audited and the notification dismissed; a zone never seen online is not
judged inside the startup grace.
"""

from datetime import timedelta

import custom_components.scout_hut_heating.coordinator as C
from custom_components.scout_hut_heating.coordinator import HEATERS_OFFLINE_MINUTES
from scout_testkit import E, ZA, ZB, make_controller, run, service_calls

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


def _rest(hass, state):
    """The office and the kitchen/toilets."""
    hass.states.set("climate.office", state, {"current_temperature": 18.0})
    for eid in E["shared"]:
        hass.states.set(eid, state, {"current_temperature": 18.0})


def _age_offline(ctrl, zone, minutes):
    ctrl._heaters_offline_since[zone] = ctrl._now() - timedelta(minutes=minutes)


def test_sustained_all_heaters_offline_is_alerted_and_recovery_audited():
    ctrl, hass = _ctrl_past_grace()
    _hall(hass, "heat")
    _rest(hass, "heat")  # only the hall goes down: a zone outage, not the token
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
    # The reload goes first; the alert waits to see whether it worked.
    assert _events(ctrl, "rointe_reload")
    assert not _events(ctrl, "heaters_offline")
    ctrl._rointe_reloaded_at -= timedelta(minutes=C.ROINTE_RELOAD_SETTLE_MIN + 1)
    run(ctrl.async_reconcile())
    outages = _events(ctrl, "heaters_offline")
    assert {e["zone"] for e in outages} == {ZA, ZB, "shared"}
    assert all(e["all_zones"] is True for e in outages)


# --- Reloading the Rointe integration ourselves (v1.51.0) -----------------------
# 2026-10-19 22:40Z: the sign-in expired 168 h after the last restart and every
# heater was dark for 6 h until a manual reload at 04:45Z.
def _everything(hass, state):
    _hall(hass, state)
    _rest(hass, state)


def _all_down(ctrl, hass, minutes=HEATERS_OFFLINE_MINUTES + 1):
    _everything(hass, "heat")
    run(ctrl.async_reconcile())
    _everything(hass, "unavailable")
    run(ctrl.async_reconcile())
    for zone in (ZA, ZB, "shared"):
        _age_offline(ctrl, zone, minutes)


def _reloads(hass):
    return service_calls(hass, "homeassistant", "reload_config_entry")


def test_every_heater_offline_reloads_the_rointe_integration():
    ctrl, hass = _ctrl_past_grace()
    _all_down(ctrl, hass)
    run(ctrl.async_reconcile())
    (call,) = _reloads(hass)
    assert set(call["data"]["entity_id"]) >= set(HALL) | {"climate.office"} | set(E["shared"])
    (reload,) = _events(ctrl, "rointe_reload")
    assert reload["attempt"] == 1 and reload["ok"] is True
    assert reload["minutes_down"] >= HEATERS_OFFLINE_MINUTES


def test_not_before_the_outage_is_ten_minutes_old():
    ctrl, hass = _ctrl_past_grace()
    _all_down(ctrl, hass, minutes=HEATERS_OFFLINE_MINUTES - 2)
    run(ctrl.async_reconcile())
    assert not _reloads(hass)


def test_one_zone_down_is_not_reloaded():
    ctrl, hass = _ctrl_past_grace()
    _everything(hass, "heat")
    run(ctrl.async_reconcile())
    _hall(hass, "unavailable")
    run(ctrl.async_reconcile())
    _age_offline(ctrl, ZA, HEATERS_OFFLINE_MINUTES + 30)
    run(ctrl.async_reconcile())
    assert not _reloads(hass)


def test_a_reload_that_works_raises_no_alert():
    ctrl, hass = _ctrl_past_grace()
    _all_down(ctrl, hass)
    run(ctrl.async_reconcile())
    _everything(hass, "heat")  # back a minute later
    run(ctrl.async_reconcile())
    assert not _events(ctrl, "heaters_offline")
    (back,) = _events(ctrl, "rointe_reload_recovered")
    assert back["attempts"] == 1
    assert ctrl._rointe_reloads == 0


def test_it_retries_hourly_then_gives_up_and_says_so(monkeypatch):
    created = []
    ctrl, hass = _ctrl_past_grace()
    monkeypatch.setattr(
        C.persistent_notification,
        "async_create",
        lambda h, m, title=None, notification_id=None: created.append(m),
    )
    _all_down(ctrl, hass)
    run(ctrl.async_reconcile())
    run(ctrl.async_reconcile())
    assert len(_reloads(hass)) == 1  # not again inside the hour
    for _ in range(C.ROINTE_RELOAD_MAX_ATTEMPTS + 2):
        ctrl._rointe_reloaded_at -= timedelta(minutes=C.ROINTE_RELOAD_RETRY_MIN + 1)
        run(ctrl.async_reconcile())
    assert len(_reloads(hass)) == C.ROINTE_RELOAD_MAX_ATTEMPTS
    assert _events(ctrl, "heaters_offline")
    assert any("reloaded automatically" in m for m in created)


def test_a_reload_that_fails_is_audited_and_retried():
    ctrl, hass = _ctrl_past_grace()
    hass.services.fail_when = lambda domain, service, data: domain == "homeassistant"
    _all_down(ctrl, hass)
    run(ctrl.async_reconcile())
    (reload,) = _events(ctrl, "rointe_reload")
    assert reload["ok"] is False and reload["error"]


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
