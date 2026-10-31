"""The learned start-up allowance (v1.51.0).

The pre-heat lead charges every climb a start-up allowance ("tail") on top of
the deficit: the time lost to the oil mass warming up and to the Rointes
throttling near target. It was a fixed 2.5 °C. October 2026 showed it too big:
the lead came out 4–23 min early on shallow climbs and 24–38 min LATE on the
two cold-soaked ones (10-21 from 11.9, 10-28 from 13.4). A straight line
through the climbs, ``rise + leak = gain × hours − tail``, puts it near
0.2–0.5. So it is learned per zone with the gain, seeded at 1.0, and the gains
learned under 2.5 are converted once (×1.375) so a 3 °C climb keeps its lead.
"""

import pytest

from custom_components.scout_hut_heating.preheat import (
    FIT_MIN_SAMPLES,
    MAX_TAIL_STEP_C,
    MIN_TAIL_C,
    TAIL_CONVERSION,
    TAIL_SEED_C,
    WARMUP_HISTORY_MAX,
    fitted_tail,
    learned_warmup,
    required_lead_minutes,
)
from scout_testkit import E, PRESET_COMFORT, ZA, advance, make_controller, run

# The October 2026 hall pre-heats: (label, start average, outdoor, hall k %/h,
# minutes to reach 19). Every one reached 19; 10-21 and 10-28 only after their
# bookings had started (the lead was short).
OCTOBER = [
    ("10-07", 15.88, 12.9, 5.55, 70),
    ("10-08", 17.12, 12.9, 5.65, 54),
    ("10-16", 16.25, 10.0, 6.48, 63),
    ("10-21", 11.88, 8.0, 7.15, 170),
    ("10-28", 13.38, 11.3, 11.67, 135),
    ("10-29", 15.0, 11.4, 13.68, 93),
    ("10-30", 17.25, 12.9, 18.63, 49),
]  # the cold-gated ones (outdoor <= 14); 10-12 and 10-14 were mild


def _climb(start, outdoor, k_pct, minutes, target=19.0):
    """(minutes, rise, avg_gap, cool_k) as `_update_warmup_learning` records it."""
    return (minutes, target - start, (start + target) / 2 - outdoor, k_pct / 100)


def _synthetic(gain_c_per_h, tail, k=0.10, outdoor=8.0, target=19.0):
    """Climbs that follow the model exactly, from four different starts."""
    out = []
    for start in (17.5, 16.0, 14.5, 13.0):
        rise = target - start
        leak = k * ((start + target) / 2 - outdoor)
        hours = (rise + tail) / (gain_c_per_h - leak)
        out.append(_climb(start, outdoor, k * 100, hours * 60, target))
    return out


# --- The fit ------------------------------------------------------------------------
def test_the_line_recovers_the_allowance_from_model_climbs():
    assert fitted_tail(_synthetic(3.5, 0.6)) == pytest.approx(0.6, abs=1e-6)
    assert fitted_tail(_synthetic(3.0, 2.0)) == pytest.approx(2.0, abs=1e-6)


def test_no_line_from_too_few_climbs_or_climbs_all_the_same_length():
    assert fitted_tail(_synthetic(3.5, 0.6)[: FIT_MIN_SAMPLES - 1]) is None
    same = [(60.0, 3.0, 5.0, 0.1)] * 5
    assert fitted_tail(same) is None


def test_before_the_line_the_gain_folds_alone_and_the_allowance_stays():
    history = _synthetic(3.5, 0.6)[:2]
    rate, tail, method = learned_warmup(16.0, 1.0, history)
    assert method == "ewma" and tail == 1.0 and rate != 16.0


def test_the_allowance_moves_at_most_half_a_degree_a_climb_and_stays_in_bounds():
    history = _synthetic(3.5, 0.0)  # a line that says "no allowance at all"
    rate, tail, method = learned_warmup(16.0, 1.5, history)
    assert method == "fit" and tail == pytest.approx(1.5 - MAX_TAIL_STEP_C)
    rate, tail, _ = learned_warmup(rate, tail, history)
    rate, tail, _ = learned_warmup(rate, tail, history)
    assert tail == MIN_TAIL_C  # 0.0 is outside the bounds


def test_the_gain_is_refitted_with_the_allowance_actually_used():
    # Held at a clamped allowance, the gain is the best line through the climbs
    # for THAT allowance, so the pair the lead uses reproduces them on average.
    history = _synthetic(3.5, 0.0)
    rate, tail, _ = learned_warmup(60 / 3.5, 1.0, history)  # tail steps 1.0 -> 0.5
    assert tail == 0.5
    leads = [
        required_lead_minutes(
            rate=rate, indoor=19.0 - r, target=19.0, outdoor=(19.0 - r + 19.0) / 2 - g,
            max_minutes=600, cool_k=k, tail=tail,
        )
        for m, r, g, k in history
    ]
    errors = [lead - m for lead, (m, _, _, _) in zip(leads, history)]
    assert abs(sum(errors) / len(errors)) < 5


def test_one_climb_cannot_move_the_gain_more_than_a_quarter():
    rate, _, _ = learned_warmup(10.0, 1.0, _synthetic(1.0, 1.0))  # a very slow line
    assert rate == pytest.approx(12.5)


# --- October, replayed ----------------------------------------------------------------
def _lead(rate, tail, start, outdoor, k_pct):
    return required_lead_minutes(
        rate=rate, indoor=start, target=19.0, outdoor=outdoor, max_minutes=600,
        cool_k=k_pct / 100, tail=tail,
    )


def test_october_replayed_the_late_cold_soaked_arrivals_are_closed():
    # The fixed 2.5 allowance at the learned 11.67-11.74 put the two cold-soaked
    # climbs 24 and 38 min late. Converted onto the 1.0 seed and folded through
    # the month in order, the learned pair leads both of them early enough, and
    # no climb on the list is more than 20 min late or 25 early.
    rate, tail = 11.74 * TAIL_CONVERSION, TAIL_SEED_C
    history = []
    for _, start, outdoor, k, minutes in OCTOBER:
        history.append(_climb(start, outdoor, k, minutes))
        rate, tail, _ = learned_warmup(rate, tail, history)
    assert MIN_TAIL_C <= tail < TAIL_SEED_C
    old, new = {}, {}
    for label, start, outdoor, k, minutes in OCTOBER:
        old[label] = _lead(11.74, 2.5, start, outdoor, k) - minutes
        new[label] = _lead(rate, tail, start, outdoor, k) - minutes
    assert old["10-21"] < -30 and old["10-28"] < -20  # late, as they were
    assert new["10-21"] > -10 and new["10-28"] > -10
    assert all(-20 <= e <= 25 for e in new.values()), new
    assert sum(e * e for e in new.values()) < sum(e * e for e in old.values())


# --- The coordinator --------------------------------------------------------------------
def _hall_reads(hass, temp):
    for climate in E["hall"]:
        hass.states.set(climate, "heat", {"current_temperature": temp})


def test_the_lead_charges_the_learned_allowance():
    ctrl, hass = make_controller()
    ctrl._numbers["hall_comfort_temp"].native_value = 22
    ctrl._numbers["zone_a_warmup_rate"].native_value = 20
    ctrl._numbers["zone_a_heatloss_pct"].native_value = 0
    _hall_reads(hass, 19)
    hass.states.set(E["weather"], "cloudy", {"temperature": 15})
    ctrl._numbers["zone_a_warmup_tail"].native_value = 0.5
    assert ctrl._zone_preheat_minutes(ZA) == round(60 * 3.5 / 3)
    assert ctrl._last_lead_calc[ZA]["tail"] == 0.5


def _preheat_climb(ctrl, hass, start, minutes, outdoor=8.0):
    ctrl._numbers["hall_comfort_temp"].native_value = 19.0
    ctrl._numbers["zone_a_heatloss_pct"].native_value = 0
    hass.states.set(E["weather"], "cloudy", {"temperature": outdoor})
    _hall_reads(hass, start)
    ctrl.applied[ZA] = PRESET_COMFORT
    ctrl._preset_reason[ZA] = ctrl._preset_entry_reason[ZA] = "preheat"
    ctrl._update_warmup_learning()
    advance(ctrl, minutes)
    _hall_reads(hass, 19.0)
    ctrl._update_warmup_learning()


def test_climbs_build_the_history_and_the_line_moves_the_allowance():
    ctrl, hass = make_controller()
    # 3.5 °C/h with a 0.4 °C allowance (no leak): (rise + 0.4) / 3.5 hours.
    for start, minutes in ((17.0, 41), (16.0, 58), (14.5, 84), (13.0, 110)):
        _preheat_climb(ctrl, hass, start, minutes)
    samples = [e for e in ctrl.audit.to_list() if e.get("event") == "warmup_sample"]
    assert [e["method"] for e in samples] == ["ewma", "ewma", "ewma", "fit"]
    assert len(ctrl._warmup_history["zone_a_warmup_rate"]) == 4
    assert samples[-1]["new_tail"] < samples[-1]["old_tail"] == 1.0
    assert ctrl.number("zone_a_warmup_tail") == samples[-1]["new_tail"]


def test_the_history_is_bounded_and_survives_a_restart():
    ctrl, hass = make_controller()
    for _ in range(WARMUP_HISTORY_MAX + 2):
        _preheat_climb(ctrl, hass, 16.0, 70)
        advance(ctrl, 30)
    history = ctrl._warmup_history["zone_a_warmup_rate"]
    assert len(history) == WARMUP_HISTORY_MAX
    snap = ctrl._state_snapshot()

    ctrl2, _ = make_controller()

    async def _load():
        return snap

    ctrl2._store.async_load = _load
    run(ctrl2._async_restore_state())
    assert ctrl2._warmup_history["zone_a_warmup_rate"] == history


# --- The one-time conversion ---------------------------------------------------------------
def _restored(data):
    ctrl, _ = make_controller()
    ctrl._numbers["zone_a_warmup_rate"].native_value = 33.23
    ctrl._numbers["zone_a_warmup_rate_fans"].native_value = 11.74
    ctrl._numbers["zone_b_warmup_rate"].native_value = 20.0
    ctrl._numbers["gents_warmup_rate"].native_value = 20.0

    async def _load():
        return data

    ctrl._store.async_load = _load
    run(ctrl._async_restore_state())
    return ctrl


def test_an_upgrade_converts_the_learned_gains_once():
    ctrl = _restored({"audit": [], "trace": []})  # a pre-v1.51.0 store
    assert ctrl.number("zone_a_warmup_rate_fans") == pytest.approx(16.14, abs=0.01)
    assert ctrl.number("zone_a_warmup_rate") == pytest.approx(45.69, abs=0.01)
    assert ctrl.number("zone_b_warmup_rate") == pytest.approx(27.5)
    assert ctrl.number("gents_warmup_rate") == 20.0  # new this release: not converted
    (evt,) = [e for e in ctrl.audit.to_list() if e.get("event") == "warmup_gain_converted"]
    assert evt["zone_a_warmup_rate_fans"] == [11.74, 16.14]
    # Saved at once, marked: a second start converts nothing.
    assert ctrl._store.saved["warmup_tail_v"] == 1
    again = _restored(ctrl._store.saved)
    assert again.number("zone_a_warmup_rate_fans") == 11.74  # as the fixture set it


def test_a_fresh_install_converts_nothing():
    ctrl = _restored(None)
    assert ctrl.number("zone_a_warmup_rate_fans") == 11.74
    assert not [e for e in ctrl.audit.to_list() if e.get("event") == "warmup_gain_converted"]
