"""Frozen ('freeze while alive') Rointe readings must not drive warm-enough
decisions cold.

The Rointe cloud can stop updating while the entity still reads `available`.
The pre-heat sizing and the unified heat gate (`_room_wants_heat`) must reject
such a frozen value rather than trust a stale-high reading and under-heat: a
dropped reading falls back to fail-warm (heat), never to "warm enough, no heat".
"""

from datetime import timedelta

from scout_testkit import (
    ZA,
    E,
    booking,
    make_controller,
    motion,
)


def _hall_temp(ctrl, temp):
    for eid in E["hall"]:
        ctrl.hass.states.set(eid, "heat", {"current_temperature": temp})


def _freeze(ctrl, minutes):
    """Age every hall heater's last_reported into the past."""
    old = ctrl._now() - timedelta(minutes=minutes)
    for eid in E["hall"]:
        st = ctrl.hass.states.get(eid)
        st.last_reported = old
        st.last_updated = old


def test_fresh_reading_is_used():
    ctrl, _ = make_controller()
    _hall_temp(ctrl, 18.0)
    assert ctrl._zone_room_temp(ZA, coldest=True, stale_min=120) == 18.0


def test_frozen_reading_is_rejected():
    ctrl, _ = make_controller()
    _hall_temp(ctrl, 18.0)
    _freeze(ctrl, 180)  # older than the 120-min stale window
    assert ctrl._zone_room_temp(ZA, coldest=True, stale_min=120) is None
    # ...but without a stale window the value is still returned (fan ΔT path).
    assert ctrl._zone_room_temp(ZA, coldest=True) == 18.0


def test_preheat_lead_fails_warm_on_frozen_reading():
    """A stale-high reading must not shrink the lead — it falls back to the cap."""
    ctrl, _ = make_controller()
    _hall_temp(ctrl, 19.4)  # reads ~at target -> would give a tiny lead if trusted
    _freeze(ctrl, 180)
    lead = ctrl._zone_preheat_minutes(ZA, gap_hours=2.0)
    assert lead == int(round(ctrl.number("preheat_minutes")))  # the cap (fail-warm)


def test_frozen_warm_reading_does_not_suppress_heat():
    """A frozen WARM reading must not read as 'warm enough, no heat'. Dropped as
    stale, the room reads None -> the gate errs warm (heat), not off."""
    ctrl, _ = make_controller()
    booking(ctrl, ZA)
    motion(ctrl, "hall")
    _hall_temp(ctrl, 20.0)  # would say 'no heat' if trusted...
    _freeze(ctrl, 180)  # ...but it is frozen
    assert ctrl._room_wants_heat(ZA, ctrl._zone_target(ZA)) is True


def test_fresh_warm_reading_suppresses_heat():
    ctrl, _ = make_controller()
    booking(ctrl, ZA)
    motion(ctrl, "hall")
    _hall_temp(ctrl, 20.0)  # fresh and warm -> no heat needed
    assert ctrl._room_wants_heat(ZA, ctrl._zone_target(ZA)) is False


def test_fresh_cold_reading_wants_heat():
    ctrl, _ = make_controller()
    booking(ctrl, ZA)
    motion(ctrl, "hall")
    _hall_temp(ctrl, 12.0)  # cold and fresh
    assert ctrl._room_wants_heat(ZA, ctrl._zone_target(ZA)) is True
