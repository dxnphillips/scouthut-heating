"""A heater its sync clock proves silent is left out of every room reading
(v1.51.1).

Field 2026-11-04: hall_left had not uploaded since 18:07Z the day before and
its reading sat at 17.0. The heat gate already dropped it (a reading frozen
past the stale window), so the hall heated on its three live probes, which
averaged 19.0 by 16:57Z for a 17:30Z booking. Everything else still counted
it: `booking_start` said the hall arrived 0.25 short (it was ~0.3 over), the
overshoot peak read 0, the trace's floor and cold end sat on the 17.0, the
drive's zone average set the approach hold and the coast band from it, and a
cool-off would have read a loss damped by a value that never falls.
"""

from datetime import timedelta

import pytest

from custom_components.scout_hut_heating.const import CONF_HALL_CLIMATES
from scout_testkit import (
    ZA,
    PRESET_COMFORT,
    make_controller,
    naive_now,
    run,
    set_registry,
    set_sync_clock,
    trust_sync_clock,
)

FRONT, BACK, LEFT, RIGHT = (
    "climate.hall_front",
    "climate.hall_back",
    "climate.hall_left",
    "climate.hall_right",
)
HALL = [FRONT, BACK, LEFT, RIGHT]
FIELD = {FRONT: 19.0, BACK: 19.5, LEFT: 17.0, RIGHT: 19.5}


def _hall(left_silent_min=25 * 60, readings=FIELD, clock=True):
    ctrl, hass = make_controller({CONF_HALL_CLIMATES: list(HALL)})
    set_registry(
        entries_by_device={f"d_{c}": [c] for c in HALL},
        entity_devices={c: f"d_{c}" for c in HALL},
    )
    for climate, temp in readings.items():
        hass.states.set(climate, "heat", {"current_temperature": temp, "temperature": 19.0})
    if clock:
        now = naive_now(ctrl)
        set_sync_clock(
            hass,
            {
                c: now - timedelta(minutes=left_silent_min if c == LEFT else 5)
                for c in HALL
            },
        )
        trust_sync_clock(ctrl)
    return ctrl, hass


def _events(ctrl, name):
    return [e for e in ctrl.audit.to_list() if e.get("event") == name]


def test_a_silent_heater_is_out_of_the_hall_reading_everywhere():
    ctrl, _ = _hall()
    assert ctrl._probe_dark(LEFT) and not ctrl._probe_dark(FRONT)
    live = (19.0 + 19.5 + 19.5) / 3
    assert ctrl._zone_room_temp(ZA) == pytest.approx(live)  # no stale_min passed
    assert ctrl._zone_room_temp(ZA, coldest=True) == 19.0
    assert ctrl._floor_temp() == pytest.approx(live)  # the trace's floor
    assert LEFT not in ctrl._room_reading_members(ZA)  # the cool-off's reading


def test_the_arrival_is_measured_on_the_live_probes():
    ctrl, _ = _hall()
    ctrl._numbers["hall_comfort_temp"].native_value = 19.0
    ctrl.applied[ZA] = PRESET_COMFORT
    ctrl._booking_began(ZA, handover=False)
    (evt,) = _events(ctrl, "booking_start")
    assert evt["coldest"] == 19.0
    assert evt["shortfall"] == pytest.approx(19.0 - (19.0 + 19.5 + 19.5) / 3, abs=0.01)
    assert evt["shortfall"] < 0  # it arrived, it was not 0.25 short


def test_a_heater_quiet_for_an_ordinary_beat_still_counts():
    ctrl, _ = _hall(left_silent_min=40)
    assert not ctrl._probe_dark(LEFT)
    assert ctrl._zone_room_temp(ZA, coldest=True) == 17.0


def test_without_a_trusted_clock_nothing_changes():
    # A flat value may be a still room; only an upload clock can say silent.
    ctrl, _ = _hall(clock=False)
    assert not ctrl._probe_dark(LEFT)
    assert ctrl._zone_room_temp(ZA) == pytest.approx(sum(FIELD.values()) / 4)


def test_the_drive_still_drives_it_but_its_value_is_out_of_the_zone_average():
    ctrl, hass = _hall(readings={FRONT: 18.5, BACK: 18.5, LEFT: 15.0, RIGHT: 18.5})
    ctrl._numbers["hall_comfort_temp"].native_value = 19.0
    ctrl.applied[ZA] = PRESET_COMFORT
    seen = {}

    def _coast(zone, target, zone_avg, comfort, now):
        seen[zone] = zone_avg
        return 0.0

    ctrl._drive_coast = _coast
    run(ctrl._reconcile_drive())
    # 15.0 is not insane against a median of 18.5 (the sanity line is 4 under),
    # so before v1.51.1 it pulled the average to 17.6 and held the approach
    # and coast bands off a room whose live probes all sat at 18.5.
    assert seen[ZA] == pytest.approx(18.5)
