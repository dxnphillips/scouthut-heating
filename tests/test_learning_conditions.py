"""Learning samples are judged on the conditions they were taken in (Q25 PR 7).

Two field cases from the 2026-09-17 export:

- A pre-dawn cold-fabric pre-heat (outdoor 13.5 at 06:00Z) was rejected as
  "mild" because the outdoor read 15.2 at the sample's END after sunrise. The
  cold gate now judges the AVERAGE outdoor over the climb, and the line sits at
  14 °C (the Q3 decision rule's "nudge toward 13-14" for autumn mornings).
- A cool-off anchored at the coast peak after a hard drive, while the panels were
  still 35 °C above the room, and read the panel shed as a 5.7x "opening" — two
  false "window/door open?" pushes in one day. The anchor now also waits for the
  zone's hottest panel surface to come within COOL_SETTLE_SURFACE_C of the room.
"""

import pytest

from custom_components.scout_hut_heating.coordinator import (
    COOL_SETTLE_MINUTES,
    COOL_SETTLE_SURFACE_C,
    WARMUP_COLD_MAX_OUTDOOR,
)
from scout_testkit import (
    E,
    PRESET_COMFORT,
    PRESET_ICE,
    ZA,
    advance,
    make_controller,
    set_registry,
)

HB, HF = "climate.hall_back", "climate.hall_front"
HB_SURFACE = "sensor.hall_back_surface_temperature"


def _events(ctrl, name):
    return [e for e in ctrl.audit.to_list() if e.get("event") == name]


def _set(ctrl, key, value):
    ctrl._numbers[key].native_value = value


def _probes(hass, temp):
    hass.states.set(HB, "heat", {"current_temperature": temp})
    hass.states.set(HF, "heat", {"current_temperature": temp})


def _outdoor(hass, temp):
    hass.states.set(E["weather"], "cloudy", {"temperature": temp})


def _wire_surface(hass, value):
    set_registry(
        entries_by_device={"dev_hall_back": [HB_SURFACE]},
        entity_devices={HB: "dev_hall_back"},
    )
    hass.states.set(HB_SURFACE, str(value))


# --- Warm-up: the cold gate reads the whole climb's outdoor -------------------
def _climb(outdoor_by_tick):
    """A clean 4 °C climb; the outdoor reading is stepped through the climb."""
    ctrl, hass = make_controller()
    _set(ctrl, "zone_a_warmup_rate", 20)
    _set(ctrl, "hall_comfort_temp", 22)
    _probes(hass, 18)
    _outdoor(hass, outdoor_by_tick[0])
    ctrl.applied[ZA] = PRESET_COMFORT
    ctrl._preset_reason[ZA] = "preheat"  # only real pre-heats are sampled
    ctrl._update_warmup_learning()
    for t, out in zip((19, 20, 21, 22), outdoor_by_tick[1:]):
        advance(ctrl, 30)
        _probes(hass, t)
        _outdoor(hass, out)
        ctrl._update_warmup_learning()
    (evt,) = _events(ctrl, "warmup_sample")
    return evt


def test_the_cold_line_sits_at_fourteen():
    assert WARMUP_COLD_MAX_OUTDOOR == 14.0


def test_pre_dawn_climb_is_not_rejected_because_the_sun_rose_by_its_end():
    # 2026-09-17: outdoor 13.0 through the dark, 15.2 at the closing tick.
    evt = _climb([13.0, 13.0, 13.5, 14.5, 15.2])
    assert evt["outdoor"] == pytest.approx((13.0 + 13.0 + 13.5 + 14.5 + 15.2) / 5)
    assert evt["mild"] is False
    assert evt["accepted"] is True


def test_a_cool_autumn_morning_at_thirteen_and_a_half_now_teaches():
    # Under the old 12 °C line this genuine cold-morning sample was thrown away.
    evt = _climb([13.5] * 5)
    assert evt["mild"] is False
    assert evt["accepted"] is True


def test_a_mild_climb_is_not_admitted_by_one_cold_closing_tick():
    # The average, not the last tick, decides — in both directions.
    evt = _climb([16.0, 16.0, 16.0, 16.0, 12.0])
    assert evt["outdoor"] == pytest.approx(15.2)
    assert evt["mild"] is True
    assert evt["accepted"] is False


def test_outdoor_lost_mid_climb_averages_the_readings_it_had():
    ctrl, hass = make_controller()
    _set(ctrl, "zone_a_warmup_rate", 20)
    _set(ctrl, "hall_comfort_temp", 22)
    _probes(hass, 18)
    _outdoor(hass, 10.0)
    ctrl.applied[ZA] = PRESET_COMFORT
    ctrl._preset_reason[ZA] = "preheat"
    ctrl._update_warmup_learning()
    for i, t in enumerate((19, 20, 21, 22)):
        advance(ctrl, 30)
        _probes(hass, t)
        if i == 1:
            hass.states.set(E["weather"], "unavailable", {})
        ctrl._update_warmup_learning()
    (evt,) = _events(ctrl, "warmup_sample")
    assert evt["outdoor"] == pytest.approx(10.0)
    assert evt["mild"] is False


# --- Cool-off: the anchor waits for the panels, not just the clock ------------
def _start_ice(ctrl, hass, temp):
    _outdoor(hass, 10)
    _probes(hass, temp)
    ctrl.applied[ZA] = PRESET_ICE
    ctrl._update_cooloff_learning()  # settle clock starts


def test_cooloff_does_not_anchor_while_a_panel_is_still_hot():
    ctrl, hass = make_controller()
    _wire_surface(hass, 55.0)  # a hard-driven panel, 35 above the room
    _start_ice(ctrl, hass, 20.0)
    advance(ctrl, COOL_SETTLE_MINUTES + 1)
    ctrl._update_cooloff_learning()
    assert ctrl._cooloff_start[ZA] is None  # time floor met, panel still hot
    advance(ctrl, 30)
    hass.states.set(HB_SURFACE, "26.0")  # still 6 above the room
    ctrl._update_cooloff_learning()
    assert ctrl._cooloff_start[ZA] is None
    advance(ctrl, 10)
    hass.states.set(HB_SURFACE, "24.0")  # within the 5 °C band
    ctrl._update_cooloff_learning()
    assert ctrl._cooloff_start[ZA] is not None
    assert ctrl._cooloff_start[ZA][1] == pytest.approx(20.0)  # anchored at the settled room


def test_cooloff_anchors_on_the_time_floor_alone_without_a_surface_sensor():
    ctrl, hass = make_controller()
    _start_ice(ctrl, hass, 20.0)
    advance(ctrl, COOL_SETTLE_MINUTES + 1)
    ctrl._update_cooloff_learning()
    assert ctrl._cooloff_start[ZA] is not None


def test_panel_shed_after_a_hard_drive_does_not_raise_the_opening_alarm():
    # The 2026-09-17 signature: room coasts up under hot panels, then sheds
    # ~1 °C in the first half hour as the panels cool — 5x the learned k.
    ctrl, hass = make_controller()
    _set(ctrl, "zone_a_heatloss_pct", 5)
    _wire_surface(hass, 60.0)
    _start_ice(ctrl, hass, 21.0)
    # Panels cool over 45 min while the room sheds fast; nothing may anchor.
    for surface, room in ((50.0, 21.0), (40.0, 20.5), (32.0, 20.0)):
        advance(ctrl, 15)
        hass.states.set(HB_SURFACE, str(surface))
        _probes(hass, room)
        ctrl._update_cooloff_learning()
        assert ctrl._cooloff_start[ZA] is None
    assert _events(ctrl, "cooloff_sample") == []
    assert ctrl._opening_inferred[ZA] is False
    # Panels settle; the fabric decay is then measured from here.
    advance(ctrl, 15)
    hass.states.set(HB_SURFACE, "24.0")
    _probes(hass, 20.0)
    ctrl._update_cooloff_learning()
    assert ctrl._cooloff_start[ZA] is not None
    assert ctrl._cooloff_start[ZA][1] == pytest.approx(20.0)


def test_zone_panels_hot_threshold():
    ctrl, hass = make_controller()
    assert ctrl._zone_panels_hot(ZA, 20.0) is False  # no surface sensor mapped
    _wire_surface(hass, 20.0 + COOL_SETTLE_SURFACE_C)
    assert ctrl._zone_panels_hot(ZA, 20.0) is False  # at the line: settled
    hass.states.set(HB_SURFACE, str(20.0 + COOL_SETTLE_SURFACE_C + 0.5))
    assert ctrl._zone_panels_hot(ZA, 20.0) is True
    hass.states.set(HB_SURFACE, "unavailable")
    assert ctrl._zone_panels_hot(ZA, 20.0) is False
