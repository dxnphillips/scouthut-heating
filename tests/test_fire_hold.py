"""Fire fallback: a panel fire holds EVERYTHING off until a person clears it.

The 230 V fan supply is hardware-cut on a fire output (the real safety). This is
the software fallback: on the alarm integration's `texecom_alerts_event` for a
fire, HA forces heating -> ice, the water heater off and the fans off, and holds
them there — surviving a restart or a power blip — until the *Clear fire hold*
button is pressed.
"""

from scout_testkit import (
    PRESET_COMFORT,
    PRESET_ICE,
    ZA,
    ZB,
    booking,
    make_controller,
    motion,
    run,
)


class _Ev:
    def __init__(self, data):
        self.data = data


def _fire(ctrl, event_type="Fire", **extra):
    ctrl._handle_fire_event(_Ev({"event_type": event_type, **extra}))


def test_fire_forces_everything_off():
    ctrl, _ = make_controller()
    # A busy, heating hut: booking + motion would normally run comfort and fans.
    booking(ctrl, ZA)
    booking(ctrl, ZB)
    motion(ctrl, "hall")
    motion(ctrl, "kitchen")

    _fire(ctrl, zone_name="Kitchen")
    assert ctrl._fire_hold is True
    assert ctrl._desired_zone(ZA) == PRESET_ICE
    assert ctrl._preset_reason[ZA] == "fire"
    assert ctrl._desired_zone(ZB) == PRESET_ICE
    assert ctrl._desired_shared() == PRESET_ICE
    assert ctrl._desired_water() is False
    assert ctrl._fan_target() == (False, None, "off")
    assert any(e["event"] == "fire" for e in ctrl.audit.to_list())


def test_fire_beats_manual_hold_and_disabled_automation():
    ctrl, _ = make_controller()
    ctrl.manual_hold[ZA] = True
    ctrl._switches["zone_b_automation_enabled"].is_on = False
    _fire(ctrl)
    # Safety wins over both a manual hold (which normally returns None) and
    # disabled automation.
    assert ctrl._desired_zone(ZA) == PRESET_ICE
    assert ctrl._desired_zone(ZB) == PRESET_ICE


def test_non_fire_event_does_not_latch():
    ctrl, _ = make_controller()
    _fire(ctrl, event_type="Arm")  # not a fire type
    assert ctrl._fire_hold is False


def test_manual_clear_releases_the_hold():
    ctrl, _ = make_controller()
    booking(ctrl, ZA)
    motion(ctrl, "hall")
    _fire(ctrl)
    assert ctrl._desired_zone(ZA) == PRESET_ICE

    run(ctrl.async_clear_fire_hold())
    assert ctrl._fire_hold is False
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT  # normal automation resumes
    assert any(e["event"] == "fire_cleared" for e in ctrl.audit.to_list())


def test_fire_hold_survives_a_restart():
    ctrl, _ = make_controller()
    _fire(ctrl)
    snap = ctrl._state_snapshot()
    assert snap["fire_hold"] is True

    # A fresh controller restoring that snapshot must come back still held.
    ctrl2, _ = make_controller()
    ctrl2._fire_hold = bool(snap["fire_hold"])  # what _async_restore_state does
    assert ctrl2._desired_zone(ZA) == PRESET_ICE


def test_repeat_fire_events_do_not_re_audit():
    ctrl, _ = make_controller()
    _fire(ctrl)
    _fire(ctrl)  # another Fire while already held
    assert len([e for e in ctrl.audit.to_list() if e["event"] == "fire"]) == 1


def test_fire_pushes_to_companion_devices():
    ctrl, hass = make_controller()
    hass.services.register("notify", "mobile_app_phone")
    _fire(ctrl)
    run(ctrl._update_fire_alarm())
    pushes = [c for c in hass.services.calls
              if c["domain"] == "notify" and c["service"] == "mobile_app_phone"]
    assert len(pushes) == 1
    assert "fire" in pushes[0]["data"]["message"].lower()
    # Once per episode.
    run(ctrl._update_fire_alarm())
    assert len([c for c in hass.services.calls if c["service"] == "mobile_app_phone"]) == 1
