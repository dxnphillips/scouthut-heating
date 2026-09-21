"""Freeze / discontinuity handling on the learning (Q25 PR 5, revised v1.43.0).

- A warm-up is timed end-to-end on the COLDEST probe with the rise clamped at
  target. A mid-climb freeze-then-catch-up with honest endpoints therefore folds
  at its honest pace (the endpoints are what the lead formula reproduces), and a
  probe unfreezing PAST target on the closing tick cannot inflate the rise. The
  two single-tick jump guards this replaced threw away the first genuinely
  cold pre-heat on record (2026-09-21, +3.24 in one tick, endpoints sound).
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


# --- Warm-up: honest endpoints teach; a jump past target is clamped ------------
def _preheat(ctrl, hass, rate=30, target=21, outdoor=5.0):
    _set(ctrl, "zone_a_warmup_rate", rate)
    _set(ctrl, "zone_a_heatloss_pct", 0)
    _set(ctrl, "hall_comfort_temp", target)
    hass.states.set(E["weather"], "cloudy", {"temperature": outdoor})  # cold: admitted
    ctrl.applied[ZA] = PRESET_COMFORT
    ctrl._preset_reason[ZA] = "preheat"


def test_a_mid_climb_freeze_with_honest_endpoints_folds_at_its_end_to_end_pace():
    # hall_front climbs steadily; hall_back (the coldest) sits frozen at 17.0 then
    # catches up to 21.0 in one tick. The sample is timed on the coldest probe
    # from its honest start to its honest arrival — 80 min for 4 °C — and folds.
    ctrl, hass = make_controller()
    _preheat(ctrl, hass)
    _probes(hass, 17.0, 17.0)
    ctrl._update_warmup_learning()
    for back, front in ((17.0, 18.0), (17.0, 19.0), (17.0, 20.0), (21.0, 21.0)):
        advance(ctrl, 20)
        _probes(hass, back, front)
        ctrl._update_warmup_learning()
    (evt,) = _events(ctrl, "warmup_sample")
    assert evt["minutes"] == pytest.approx(80, abs=0.1)
    assert evt["rise"] == pytest.approx(4.0)
    assert evt["accepted"] is True
    assert evt["new_rate"] < evt["old_rate"]  # (4 + 2.5) / 1.33 h -> 12.3 min/°C


def test_a_probe_unfreezing_past_target_has_its_rise_clamped():
    # The closing tick overshoots the target by 3 °C (the catch-up landing high):
    # the rise is judged to target, so the overshoot cannot make the climb read
    # faster than it was.
    ctrl, hass = make_controller()
    _preheat(ctrl, hass)
    _probes(hass, 17.0, 17.0)
    ctrl._update_warmup_learning()
    for t in (18.0, 19.0, 20.0):
        advance(ctrl, 30)
        _probes(hass, t, t)
        ctrl._update_warmup_learning()
    advance(ctrl, 30)
    _probes(hass, 24.0, 24.0)
    ctrl._update_warmup_learning()
    (evt,) = _events(ctrl, "warmup_sample")
    assert evt["end_temp"] == 24.0  # what the probe said...
    assert evt["rise"] == pytest.approx(4.0)  # ...judged to the 21 target
    assert evt["accepted"] is True


def test_a_steady_climb_on_every_probe_still_teaches():
    ctrl, hass = make_controller()
    _preheat(ctrl, hass)
    _probes(hass, 17.0, 17.0)
    ctrl._update_warmup_learning()
    for t in (18.0, 19.0, 20.0, 21.0):
        advance(ctrl, 30)
        _probes(hass, t, t)
        ctrl._update_warmup_learning()
    (evt,) = _events(ctrl, "warmup_sample")
    assert evt["accepted"] is True
    assert evt["observed_gain"] == pytest.approx(60 / ((4 + 2.5) / 2), abs=0.01)


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
