"""A hall probe that cannot be reading the room is left out of it (v1.50.6).

Field 2026-10-23 06:27Z: hall_left read 8.0 beside its own 82 °C panel while
the other three hall heaters read 18.5–19.5. The drive already called it insane,
but the hall's room reading (the average the heating, the pre-heat and the fans
all judge on) still counted it, and said 16.25 for a room at 19.
"""

import custom_components.scout_hut_heating.coordinator as C
from custom_components.scout_hut_heating.const import CONF_HALL_CLIMATES
from scout_testkit import (
    PRESET_COMFORT,
    PRESET_ICE,
    ZA,
    advance,
    booking,
    make_controller,
    motion,
    run,
)

FRONT, BACK, LEFT, RIGHT = (
    "climate.hall_front",
    "climate.hall_back",
    "climate.hall_left",
    "climate.hall_right",
)
HALL = [FRONT, BACK, LEFT, RIGHT]


def _hall(readings):
    ctrl, hass = make_controller({CONF_HALL_CLIMATES: list(HALL)})
    _set(hass, readings)
    return ctrl, hass


def _set(hass, readings):
    for climate, temp in readings.items():
        if temp is None:
            hass.states.set(climate, "unavailable", {})
        else:
            hass.states.set(climate, "heat", {"current_temperature": temp})


def _events(ctrl, name):
    return [e for e in ctrl.audit.to_list() if e.get("event") == name]


FIELD = {FRONT: 18.5, BACK: 19.0, LEFT: 8.0, RIGHT: 19.5}


def test_a_probe_far_below_its_siblings_is_left_out_of_the_hall_reading():
    ctrl, _ = _hall(FIELD)
    assert ctrl._hall_implausible_probes() == {LEFT}
    assert ctrl._zone_room_temp(ZA) == 19.0  # 18.5, 19.0 and 19.5; not 16.25
    assert ctrl._zone_room_temp(ZA, coldest=True) == 18.5
    assert ctrl._floor_temp() == 19.0  # the fans read the same hall


def test_an_ordinary_spread_along_the_hall_is_kept():
    # The door end three degrees down (2026-10-21's changeover traffic) is a
    # real cold end, not a broken probe.
    ctrl, _ = _hall({FRONT: 15.5, BACK: 18.5, LEFT: 18.5, RIGHT: 19.0})
    assert ctrl._hall_implausible_probes() == set()
    assert ctrl._zone_room_temp(ZA) == 17.875


def test_two_readable_probes_are_never_judged():
    # With two there is no telling which one is wrong.
    ctrl, _ = _hall({FRONT: None, BACK: 19.0, LEFT: 8.0, RIGHT: None})
    assert ctrl._hall_implausible_probes() == set()
    assert ctrl._zone_room_temp(ZA) == 13.5


def test_a_booked_hall_with_a_broken_probe_releases_on_the_others():
    ctrl, _ = _hall({FRONT: 19.5, BACK: 19.5, LEFT: 8.0, RIGHT: 19.5})
    ctrl._numbers["hall_comfort_temp"].native_value = 19.0
    ctrl._numbers["booking_hold_cap"].native_value = 0.0
    booking(ctrl, ZA)
    motion(ctrl, "hall")
    # Counting the 8.0 the hall averaged 16.6 and could never reach its 19.5
    # release while the broken heater ran flat out.
    assert ctrl._desired_zone(ZA) == PRESET_ICE
    assert ctrl._preset_reason[ZA] == "booking_warm"


def test_a_cold_hall_with_a_broken_probe_still_heats():
    ctrl, _ = _hall({FRONT: 17.0, BACK: 17.5, LEFT: 8.0, RIGHT: 17.5})
    booking(ctrl, ZA)
    motion(ctrl, "hall")
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT


def test_a_lasting_implausible_probe_is_alerted_and_cleared(monkeypatch):
    created, dismissed = [], []
    monkeypatch.setattr(
        C.persistent_notification,
        "async_create",
        lambda hass, msg, **k: created.append((k.get("notification_id"), msg)),
    )
    monkeypatch.setattr(
        C.persistent_notification,
        "async_dismiss",
        lambda hass, nid: dismissed.append(nid),
    )
    ctrl, hass = _hall(FIELD)
    run(ctrl.async_reconcile())
    assert LEFT in ctrl._probe_implausible_since
    assert not _events(ctrl, "probe_implausible")  # a brief catch-up is not news

    advance(ctrl, C.PROBE_IMPLAUSIBLE_ALERT_MINUTES + 1)
    run(ctrl.async_reconcile())
    (evt,) = _events(ctrl, "probe_implausible")
    assert evt["heater"] == LEFT and evt["reading"] == 8.0 and evt["median"] == 19.0
    assert created and created[-1][0] == C.NOTIFY_PROBE_IMPLAUSIBLE
    assert "8.0" in created[-1][1]

    run(ctrl.async_reconcile())
    assert len(_events(ctrl, "probe_implausible")) == 1  # once per episode

    # Dropping off the cloud is not a recovery.
    _set(hass, {LEFT: None})
    run(ctrl.async_reconcile())
    assert not _events(ctrl, "probe_plausible")

    _set(hass, {LEFT: 19.0})
    run(ctrl.async_reconcile())
    (back,) = _events(ctrl, "probe_plausible")
    assert back["heater"] == LEFT and back["reading"] == 19.0
    assert C.NOTIFY_PROBE_IMPLAUSIBLE in dismissed
    assert not ctrl._probe_implausible_since
