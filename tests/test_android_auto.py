"""Urgent companion pushes carry Android Auto (car_ui) data.

The two alerts that reach `_push_companion` — a fire hold and an inferred
open window/door — are exactly the ones worth seeing while driving, so each
push must include the Android Auto payload (car_ui + icon + channel + high
priority) on top of the plain title/message.
"""

from scout_testkit import ZB, make_controller, run


class _Ev:
    def __init__(self, data):
        self.data = data


def _pushes(hass):
    return [
        c
        for c in hass.services.calls
        if c["domain"] == "notify" and c["service"] == "mobile_app_phone"
    ]


def test_fire_push_is_android_auto_visible():
    ctrl, hass = make_controller()
    hass.services.register("notify", "mobile_app_phone")
    ctrl._handle_fire_event(_Ev({"event_type": "Fire"}))
    run(ctrl._update_fire_alarm())

    pushes = _pushes(hass)
    assert len(pushes) == 1
    payload = pushes[0]["data"]
    assert "fire" in payload["message"].lower()  # title/message still sent
    car = payload["data"]
    assert car["car_ui"] is True
    assert car["notification_icon"] == "mdi:fire"
    assert car["channel"] == "Scout Hut Fire"
    assert car["importance"] == "high"
    assert car["ttl"] == 0 and car["priority"] == "high"


def test_opening_push_is_android_auto_visible():
    ctrl, hass = make_controller()
    ctrl._numbers["zone_b_heatloss_pct"].write_value(5.0)  # learned baseline
    hass.services.register("notify", "mobile_app_phone")

    # An out-of-family cool-off (6.7x the baseline) infers an open window.
    outdoor = (21.0 + 20.0) / 2 - 6.0
    ctrl._fold_cooloff(ZB, 0.5, 1.0, 21.0, 20.0, outdoor, 1, 0, 40, 0, 0, 0.5)
    assert ctrl._opening_inferred[ZB] is True
    run(ctrl._update_opening_inferred_alarm())

    pushes = _pushes(hass)
    assert len(pushes) == 1
    car = pushes[0]["data"]["data"]
    assert car["car_ui"] is True
    assert car["notification_icon"] == "mdi:door-open"
    assert car["channel"] == "Scout Hut Openings"
    assert car["priority"] == "high"


def test_no_companion_app_is_a_safe_no_op():
    ctrl, hass = make_controller()  # no mobile_app_* registered
    ctrl._handle_fire_event(_Ev({"event_type": "Fire"}))
    run(ctrl._update_fire_alarm())
    assert _pushes(hass) == []
