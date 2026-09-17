"""Per-probe freeze / discontinuity guards on the learning (Q25 PR 5).

- A warm-up sample is rejected when any ONE probe jumps a tick's worth of
  freeze-then-catch-up, even though the zone average dilutes it below the guard
  (2026-09-16: a +4.0 on one of four probes read as +1.0 on the average).
- An out-of-family cool-off whose reading sat flat for a long spell is the probe
  freeze-then-catch-up, not an opening: rejected, but the "window/door open?"
  latch is not raised (the office's recurring 3am false alarm).
"""

from datetime import timedelta

import pytest

from custom_components.scout_hut_heating.coordinator import (
    COOL_FREEZE_FLAT_MINUTES,
    COOL_SETTLE_MINUTES,
)
from scout_testkit import E, PRESET_COMFORT, PRESET_ICE, ZA, advance, make_controller

HB, HF = "climate.hall_back", "climate.hall_front"


def _events(ctrl, name):
    return [e for e in ctrl.audit.to_list() if e.get("event") == name]


def _set(ctrl, key, value):
    ctrl._numbers[key].native_value = value


def _probes(hass, back, front):
    hass.states.set(HB, "heat", {"current_temperature": back})
    hass.states.set(HF, "heat", {"current_temperature": front})


# --- Warm-up: a single probe's jump is not diluted by the average ------------
def test_single_probe_freeze_jump_rejects_the_warmup_sample():
    ctrl, hass = make_controller()
    _set(ctrl, "zone_a_warmup_rate", 30)
    _set(ctrl, "hall_comfort_temp", 21)
    hass.states.set(E["weather"], "cloudy", {"temperature": 5.0})  # cold: the gate admits it
    _probes(hass, 17.0, 17.0)
    ctrl.applied[ZA] = PRESET_COMFORT
    ctrl._update_warmup_learning()
    # hall_front climbs steadily; hall_back sits frozen at 17.0 then jumps +4.0
    # in one tick — only +1.0 on the two-probe average, under the 1.5 guard.
    for back, front in ((17.0, 18.0), (17.0, 19.0), (17.0, 20.0), (21.0, 21.0)):
        advance(ctrl, 20)
        _probes(hass, back, front)
        ctrl._update_warmup_learning()
    (evt,) = _events(ctrl, "warmup_sample")
    assert evt["max_tick_rise"] == pytest.approx(2.5)  # the average's largest step
    assert evt["max_probe_tick_rise"] == pytest.approx(4.0)  # the single probe's
    assert evt["accepted"] is False
    assert evt["new_rate"] == evt["old_rate"] == 30.0


def test_a_steady_climb_on_every_probe_still_teaches():
    ctrl, hass = make_controller()
    _set(ctrl, "zone_a_warmup_rate", 30)
    _set(ctrl, "hall_comfort_temp", 21)
    hass.states.set(E["weather"], "cloudy", {"temperature": 5.0})
    _probes(hass, 17.0, 17.0)
    ctrl.applied[ZA] = PRESET_COMFORT
    ctrl._update_warmup_learning()
    for t in (18.0, 19.0, 20.0, 21.0):
        advance(ctrl, 30)
        _probes(hass, t, t)
        ctrl._update_warmup_learning()
    (evt,) = _events(ctrl, "warmup_sample")
    assert evt["max_probe_tick_rise"] == pytest.approx(1.0)
    assert evt["accepted"] is True


# --- Cool-off: a long flat spell before an out-of-family drop is a freeze ----
def _begin_cooloff(ctrl, hass, temp):
    hass.states.set(E["weather"], "cloudy", {"temperature": 10})
    _probes(hass, temp, temp)
    ctrl.applied[ZA] = PRESET_ICE
    ctrl._update_cooloff_learning()  # settle clock starts
    advance(ctrl, COOL_SETTLE_MINUTES + 1)
    ctrl._update_cooloff_learning()  # anchored
    assert ctrl._cooloff_start[ZA] is not None


def test_frozen_then_catch_up_is_rejected_without_the_opening_alarm():
    ctrl, hass = make_controller()
    _set(ctrl, "zone_a_heatloss_pct", 1)  # a well-learned, tight (insulated) baseline
    _begin_cooloff(ctrl, hass, 20.0)
    # The reading holds 20.0 for two hours (the probe froze)...
    for _ in range(4):
        advance(ctrl, 30)
        ctrl._update_cooloff_learning()
    # ...then dumps the catch-up: a whole degree in one step, read over the
    # window as ~5x the fabric's loss — the office's 3am signature.
    advance(ctrl, 10)
    _probes(hass, 19.0, 19.0)
    ctrl._update_cooloff_learning()
    evt = _events(ctrl, "cooloff_sample")[0]
    assert evt["outlier"] is True
    assert evt["frozen"] is True
    assert evt["max_flat_min"] >= COOL_FREEZE_FLAT_MINUTES
    assert evt["accepted"] is False
    assert evt["new_pct"] == evt["old_pct"] == 1.0  # k untouched
    assert ctrl._opening_inferred[ZA] is False  # no "window/door open?" push


def test_an_out_of_family_drop_without_a_flat_spell_still_raises_the_alarm():
    ctrl, hass = make_controller()
    _set(ctrl, "zone_a_heatloss_pct", 2)
    _begin_cooloff(ctrl, hass, 20.0)
    # A steady, fast, stepping decay: the reading never sits still — an opening.
    for t in (19.75, 19.5, 19.25, 19.0, 18.75, 18.5):
        advance(ctrl, 15)
        _probes(hass, t, t)
        ctrl._update_cooloff_learning()
    samples = _events(ctrl, "cooloff_sample")
    assert samples and samples[-1]["outlier"] is True
    assert samples[-1]["frozen"] is False
    assert samples[-1]["max_flat_min"] < COOL_FREEZE_FLAT_MINUTES
    assert ctrl._opening_inferred[ZA] is True


def test_a_slow_genuine_decay_with_long_dwells_is_still_learned():
    # An insulated room legitimately dwells an hour or two on one quantum; its
    # decay is in-family, so the flat test never touches it.
    ctrl, hass = make_controller()
    _set(ctrl, "zone_a_heatloss_pct", 5)
    _begin_cooloff(ctrl, hass, 20.0)
    for t in (20.0, 20.0, 19.5, 19.5, 19.5, 19.5, 19.0):
        advance(ctrl, 60)
        _probes(hass, t, t)
        ctrl._update_cooloff_learning()
    samples = _events(ctrl, "cooloff_sample")
    assert samples and samples[-1]["accepted"] is True
    assert samples[-1]["frozen"] is False
    assert ctrl._opening_inferred[ZA] is False


def test_the_advance_helper_ages_the_flat_clock():
    # Guard for the test kit itself: the in-sample flat clock must move with
    # simulated time or the freeze signature could never be exercised.
    ctrl, hass = make_controller()
    _begin_cooloff(ctrl, hass, 20.0)
    before = ctrl._cooloff_start[ZA][10]
    advance(ctrl, 30)
    assert ctrl._cooloff_start[ZA][10] == before - timedelta(minutes=30)
