"""Inferred unsensored opening: protect the learned heat-loss AND alert.

Once a zone's heat-loss EWMA is learned down to a real baseline, a cool-off
implying a loss rate far above it is not the fabric — it is an open window/door
with no contact to raise the opening guard (the office's standing blind spot).
The sample must be rejected (not fold into the constant) and a push sent to every
companion-app device.
"""

from scout_testkit import ZB, make_controller, run


def _fold(ctrl, *, hours, drop, gap, tick=0.5):
    # gap = mean(start,end) - outdoor; keep start-end == drop.
    start, end = 21.0, 21.0 - drop
    outdoor = (start + end) / 2 - gap
    ctrl._fold_cooloff(ZB, hours, drop, start, end, outdoor, 1, 0, 40, 0, 0, tick)


def _events(ctrl, kind):
    return [e for e in ctrl.audit.to_list() if e.get("event") == kind]


def test_outlier_office_cooloff_is_rejected_and_pushed():
    ctrl, hass = make_controller()
    ctrl._numbers["zone_b_heatloss_pct"].write_value(5.0)  # learned baseline 5 %/h
    hass.services.register("notify", "mobile_app_phone")

    # 1.0 °C in 0.5 h at gap 6 -> 33 %/h, 6.7x the baseline: an open window.
    _fold(ctrl, hours=0.5, drop=1.0, gap=6.0)
    assert ctrl._opening_inferred[ZB] is True
    assert round(ctrl.number("zone_b_heatloss_pct"), 2) == 5.0  # NOT corrupted
    sample = _events(ctrl, "cooloff_sample")[-1]
    assert sample["outlier"] is True and sample["accepted"] is False

    run(ctrl._update_opening_inferred_alarm())
    assert ZB in ctrl._opening_notified
    assert len(_events(ctrl, "opening_inferred")) == 1
    pushes = [c for c in hass.services.calls
              if c["domain"] == "notify" and c["service"] == "mobile_app_phone"]
    assert len(pushes) == 1
    assert "open" in pushes[0]["data"]["message"].lower()

    # A second reconcile does not re-push (once per episode).
    run(ctrl._update_opening_inferred_alarm())
    assert len([c for c in hass.services.calls if c["service"] == "mobile_app_phone"]) == 1


def test_in_family_sample_clears_the_latch():
    ctrl, hass = make_controller()
    ctrl._numbers["zone_b_heatloss_pct"].write_value(5.0)
    hass.services.register("notify", "mobile_app_phone")

    _fold(ctrl, hours=0.5, drop=1.0, gap=6.0)  # outlier -> latch
    run(ctrl._update_opening_inferred_alarm())
    assert ZB in ctrl._opening_notified

    # A normal, in-family cool-off (window closed) clears the latch.
    _fold(ctrl, hours=2.0, drop=1.0, gap=6.0)  # ~8 %/h, in family
    assert ctrl._opening_inferred[ZB] is False
    run(ctrl._update_opening_inferred_alarm())
    assert ZB not in ctrl._opening_notified


def test_no_companion_app_is_a_noop_not_an_error():
    # No mobile_app_* services registered: the push is a no-op, the persistent
    # notification path still runs, nothing raises.
    ctrl, hass = make_controller()
    ctrl._numbers["zone_b_heatloss_pct"].write_value(5.0)
    _fold(ctrl, hours=0.5, drop=1.0, gap=6.0)
    run(ctrl._update_opening_inferred_alarm())
    assert ZB in ctrl._opening_notified
    assert not [c for c in hass.services.calls if c["domain"] == "notify"]
