"""Inferred unsensored opening: protect the learned heat-loss AND alert.

Once a zone's heat-loss EWMA is learned down to a real baseline, a cool-off
implying a loss rate far above it is not the fabric — it is an open window/door
with no contact to raise the opening guard (the office's standing blind spot).
The sample must be rejected (not fold into the constant) and a push sent to every
companion-app device.
"""

from scout_testkit import ZB, make_controller, run


def _fold(ctrl, *, hours, drop, gap, tick=0.5, settled=None):
    # gap = mean(start,end) - outdoor; keep start-end == drop.
    start, end = 21.0, 21.0 - drop
    outdoor = (start + end) / 2 - gap
    ctrl._fold_cooloff(
        ZB, hours, drop, start, end, outdoor, 1, 0, 40, 0, 0, tick, settled_min=settled
    )


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


def test_an_outlier_inside_the_settle_window_is_a_transient_not_an_opening():
    # 2026-10-07/08: four "window/door open?" pushes in 24 h with nothing open.
    # Each sample anchored 48-64 min after the zone went to ice and read the air
    # re-equilibrating with a cold fabric (15-20 %/h for the first hour, then 11,
    # 8, 5) as 3-5x the slow-phase baseline. Rejected, but the latch is untouched.
    from custom_components.scout_hut_heating.coordinator import COOL_ALARM_SETTLE_MIN

    ctrl, hass = make_controller()
    ctrl._numbers["zone_b_heatloss_pct"].write_value(3.78)
    hass.services.register("notify", "mobile_app_phone")
    _fold(ctrl, hours=0.5, drop=1.0, gap=10.94, settled=62)  # the 21:18Z office sample
    sample = _events(ctrl, "cooloff_sample")[-1]
    assert sample["outlier"] is True and sample["accepted"] is False
    assert sample["transient"] is True and sample["settled_min"] == 62
    assert ctrl._opening_inferred[ZB] is False
    assert round(ctrl.number("zone_b_heatloss_pct"), 2) == 3.78  # still not folded
    run(ctrl._update_opening_inferred_alarm())
    assert not [c for c in hass.services.calls if c["domain"] == "notify"]
    # The same decay anchored past the line IS judged as an opening.
    _fold(ctrl, hours=0.5, drop=1.0, gap=10.94, settled=COOL_ALARM_SETTLE_MIN)
    assert _events(ctrl, "cooloff_sample")[-1]["transient"] is False
    assert ctrl._opening_inferred[ZB] is True


def test_a_transient_does_not_clear_a_standing_latch_either():
    ctrl, hass = make_controller()
    ctrl._numbers["zone_b_heatloss_pct"].write_value(5.0)
    _fold(ctrl, hours=0.5, drop=1.0, gap=6.0, settled=200)  # a real opening: latched
    assert ctrl._opening_inferred[ZB] is True
    _fold(ctrl, hours=0.5, drop=1.0, gap=6.0, settled=30)  # says nothing either way
    assert ctrl._opening_inferred[ZB] is True


def test_no_companion_app_is_a_noop_not_an_error():
    # No mobile_app_* services registered: the push is a no-op, the persistent
    # notification path still runs, nothing raises.
    ctrl, hass = make_controller()
    ctrl._numbers["zone_b_heatloss_pct"].write_value(5.0)
    _fold(ctrl, hours=0.5, drop=1.0, gap=6.0)
    run(ctrl._update_opening_inferred_alarm())
    assert ZB in ctrl._opening_notified
    assert not [c for c in hass.services.calls if c["domain"] == "notify"]
