"""Adaptive pre-heat (optimum start): pure model + coordinator wiring.

The lead is a one-node heat balance (v1.43.0), not a linear "minutes per
degree": gross radiator gain minus the fabric leak the cool-off learning already
measures, spread over the deficit PLUS a fixed radiator finish. Every hall climb
on record draws that shape (minutes ≈ 31 + 10 × rise), and the finish is what
makes a 4 °C cold start and a 1 °C top-up read as ONE family of gains — so the
old out-of-family gate and the two single-tick jump guards are gone, replaced by
which climbs get sampled at all (a real pre-heat, timed on the coldest probe,
that reached target, in cold conditions).
"""

from datetime import timedelta

import pytest

from custom_components.scout_hut_heating.preheat import (
    APPROACH_TAIL_C,
    MIN_LEAD,
    MIN_SAMPLE_RISE,
    net_gain_c_per_h,
    predicted_room_temp,
    required_lead_minutes,
    updated_rate,
    warmup_observed_rate,
)
from scout_testkit import PRESET_COMFORT, PRESET_ECO, ZA, ZB, advance, make_controller, E

from custom_components.scout_hut_heating.coordinator import (
    COOL_SETTLE_MINUTES,
    DRIVE_STARTUP_GRACE_MINUTES,
)


def _begin_cooloff(ctrl):
    """Anchor a cool-off past the post-heating settle delay (temp held constant
    across the wait by the caller, so the anchor lands on the intended start)."""
    ctrl._update_cooloff_learning()
    advance(ctrl, COOL_SETTLE_MINUTES)
    ctrl._update_cooloff_learning()


# --- Pure model: the lead ---------------------------------------------------------

def test_lead_is_deficit_plus_finish_over_net_gain():
    # 3 °C short at a gross gain of 20 min/°C (3 °C/h) with no leak: the deficit
    # plus the 2.5 °C finish at 3 °C/h -> 110 min. (The old linear formula said
    # 60 and was under-leading exactly this kind of top-up.)
    assert required_lead_minutes(
        rate=20, indoor=19, target=22, outdoor=15, max_minutes=240
    ) == pytest.approx(60 * (3 + APPROACH_TAIL_C) / 3)


def test_lead_is_capped_by_the_slider():
    assert required_lead_minutes(
        rate=20, indoor=12, target=22, outdoor=15, max_minutes=120
    ) == 120


def test_unknown_room_falls_back_to_the_cap():
    assert required_lead_minutes(
        rate=20, indoor=None, target=22, outdoor=15, max_minutes=120
    ) == 120


def test_warm_room_still_gets_the_minimum_lead():
    assert required_lead_minutes(
        rate=20, indoor=23, target=22, outdoor=15, max_minutes=120
    ) == MIN_LEAD


def test_cold_weather_margin_comes_from_the_measured_leak():
    # The same climb on a cold night is charged the fabric leak at the mid-climb
    # gap (k × gap), from the SAME learned k the cool-off measures — not a
    # guessed 1 %/°C multiplier. Outdoor 15 vs 5 at k 0.10, rate 20 (3 °C/h):
    mild = required_lead_minutes(
        rate=20, indoor=19, target=22, outdoor=15, max_minutes=600, cool_k=0.10
    )
    cold = required_lead_minutes(
        rate=20, indoor=19, target=22, outdoor=5, max_minutes=600, cool_k=0.10
    )
    assert cold > mild
    assert cold == pytest.approx(60 * 5.5 / (3 - 0.10 * (20.5 - 5)))


def test_a_leak_that_eats_the_gain_pins_the_cap():
    # Unreachable at this outdoor -> the cap (warm), never a negative or absurd lead.
    assert required_lead_minutes(
        rate=60, indoor=10, target=19, outdoor=-5, max_minutes=240, cool_k=0.25
    ) == 240


def test_unlearned_seed_pins_the_cap_even_on_a_small_deficit():
    # MAX_RATE (60) is 1 °C/h gross: with any leak at all the net is ~zero, so an
    # unlearned zone falls back to the cap — the fail-warm default, as before.
    assert required_lead_minutes(
        rate=60, indoor=18, target=19, outdoor=10, max_minutes=240, cool_k=0.10
    ) == 240


def test_net_gain_and_prediction_helpers():
    assert net_gain_c_per_h(20, 19, 22, 15, 0.0) == pytest.approx(3.0)
    assert net_gain_c_per_h(20, 19, 22, 5, 0.10) == pytest.approx(3 - 0.10 * 15.5)
    assert net_gain_c_per_h(20, 19, 22, None, 0.10) < net_gain_c_per_h(20, 19, 22, 15, 0.10)
    assert predicted_room_temp(19, 15, None, 0.25) == 19  # no gap -> now
    assert predicted_room_temp(19, 15, 6, 0.0) == 19  # no learned k -> now
    assert 15 < predicted_room_temp(19, 15, 6, 0.25) < 19


# --- Pure model: the learning ------------------------------------------------------

def test_rate_update_moves_toward_the_observation():
    # 4 °C in 195 min: with the 2.5 °C finish added back that is 6.5 °C over
    # 3.25 h = 2 °C/h gross = 30 min/°C. EWMA (alpha 0.3) from 20 -> 23.
    assert updated_rate(20, 195, 4) == pytest.approx(23)


def test_rate_update_ignores_small_rises():
    # Under the finish model a sub-2 °C climb is mostly finish and teaches
    # nothing about the slope.
    assert MIN_SAMPLE_RISE == 2.0
    assert updated_rate(20, 30, 0.4) == 20
    assert updated_rate(20, 90, 1.5) == 20


def test_rate_update_clamps_wild_observations():
    # 2 °C in 600 min -> 0.45 °C/h -> 133 min/°C; the observation clamps to 60,
    # alpha would pull 20 -> 32, but the 25 % robust step cap holds it to 25.
    assert updated_rate(20, 600, 2) == pytest.approx(25.0)


def test_rate_update_adds_the_leak_back():
    # The same climb on a cold night (big gap) implies a BIGGER gross gain: the
    # radiators were also paying the leak. So the learned rate lands LOWER
    # (faster) than the no-leak reading of the same minutes and rise.
    no_leak = warmup_observed_rate(120, 4, avg_gap=0.0, cool_k=0.0)
    leaky = warmup_observed_rate(120, 4, avg_gap=15.0, cool_k=0.10)
    assert leaky < no_leak
    assert leaky == pytest.approx(60 / (6.5 / 2 + 1.5))


def test_warmup_single_sample_cannot_yank_the_rate():
    # A low learned rate (10) must not be yanked up by one anomalously slow
    # sample: 2 °C in 120 min reads 26.7; alpha would fold to 15, the 25 % step
    # cap holds it to 12.5 (a slow sample is fail-safe, but still shouldn't leap).
    assert updated_rate(10.0, 120.0, 2.0) == pytest.approx(12.5)


def test_a_lead_sized_from_a_folded_rate_reproduces_the_climb():
    # Round trip: learn the gain from a climb, size a lead for the same climb
    # under the same conditions, get the same minutes back.
    minutes, rise, gap, k = 79.1, 4.12, 7.6, 0.0985
    gain = warmup_observed_rate(minutes, rise, gap, k)
    lead = required_lead_minutes(
        rate=gain, indoor=19.0 - rise, target=19.0, outdoor=(19.0 - rise + 19.0) / 2 - gap,
        max_minutes=600, cool_k=k,
    )
    assert lead == pytest.approx(minutes, abs=0.2)


def test_the_field_climbs_read_as_one_family_of_gains():
    # The two real cold pre-heats on record (2026-09-17 pre-dawn, 2026-09-21),
    # as the zone average recorded them, land in 10-13 min/°C gross once the
    # finish and the leak are taken out — despite reading 21.4 and 19.2 plain
    # min/°C. The 09-16 BOOST (setpoint +2, elements never throttle) would read
    # ~7, out of family: that is why only `preheat` climbs are sampled.
    k = 0.0985
    predawn = warmup_observed_rate(77.5, 3.62, (15.38 + 19.0) / 2 - 13.5, k)
    today = warmup_observed_rate(79.1, 4.12, (14.88 + 19.0) / 2 - 9.34, k)
    boost = warmup_observed_rate(47.4, 3.87, (15.38 + 19.25) / 2 - 10.9, k)
    assert 10 <= predawn <= 13
    assert 10 <= today <= 13
    assert boost < 8


def test_small_deficits_are_never_led_shorter_than_before():
    # At the day-one reset (15) with the hall's k, a top-up gets a LONGER lead
    # than the old 40 min/°C × 1 %/°C formula for every deficit up to ~2 °C —
    # the finish is charged — so no near-target arrival is colder than today.
    for deficit in (0.5, 1.0, 1.5):
        new = required_lead_minutes(
            rate=15, indoor=19 - deficit, target=19, outdoor=10, max_minutes=600, cool_k=0.0985
        )
        old = 40 * deficit * (1 + (15 - 10) * 0.01)
        assert new >= old


def test_deep_cold_pins_the_cap_at_the_reset_rate():
    # Nothing below the fitted range (outdoor < 9, rise > 4.5) is measured, so
    # on day one those mornings behave exactly as the cap-pinned design did.
    assert required_lead_minutes(
        rate=15, indoor=10, target=19, outdoor=3, max_minutes=240, cool_k=0.0985
    ) == 240


# --- Coordinator wiring ----------------------------------------------------------

def _hall_temp(hass, temp):
    hass.states.set(E["hall"][0], "heat", {"current_temperature": temp})


def _set_rate(ctrl, key, value):
    ctrl._numbers[key].native_value = value


def _start_preheat(ctrl):
    """Put the zone into comfort the way a pre-heat window does."""
    ctrl.applied[ZA] = PRESET_COMFORT
    ctrl._preset_reason[ZA] = "preheat"


def test_zone_lead_uses_room_and_weather():
    ctrl, hass = make_controller()
    _set_rate(ctrl, "hall_comfort_temp", 22)  # pin: tests the maths, not the default
    _set_rate(ctrl, "zone_a_warmup_rate", 20)
    _set_rate(ctrl, "zone_a_heatloss_pct", 0)  # isolate the deficit term
    _hall_temp(hass, 19)
    hass.states.set(E["weather"], "cloudy", {"temperature": 15})
    assert ctrl._zone_preheat_minutes(ZA) == 110  # (3 + 2.5) / (3 °C/h)


def test_zone_lead_audits_its_heat_balance_terms():
    ctrl, hass = make_controller()
    _set_rate(ctrl, "hall_comfort_temp", 22)
    _set_rate(ctrl, "zone_a_warmup_rate", 20)
    _set_rate(ctrl, "zone_a_heatloss_pct", 10)
    _hall_temp(hass, 19)
    hass.states.set(E["weather"], "cloudy", {"temperature": 5})
    ctrl._zone_preheat_minutes(ZA)
    calc = ctrl._last_lead_calc[ZA]
    assert calc["predicted"] == 19.0
    assert calc["net_c_per_h"] == pytest.approx(3 - 0.10 * (20.5 - 5), abs=0.001)


def test_unlearned_zone_uses_the_full_cap():
    # The rates are seeded at the slowest plausible value, so before any
    # learning a normal cold start computes past the cap and clamps to it —
    # fail-safe warm, exactly the old fixed behaviour.
    ctrl, hass = make_controller()
    _hall_temp(hass, 17)
    assert ctrl._zone_preheat_minutes(ZA) == 120


def test_zone_lead_without_room_reading_is_the_cap():
    ctrl, _ = make_controller()
    assert ctrl._zone_preheat_minutes(ZA) == 120


def test_eco_booking_aims_at_the_eco_low_target():
    ctrl, hass = make_controller()
    _set_rate(ctrl, "zone_a_warmup_rate", 20)
    _set_rate(ctrl, "zone_a_heatloss_pct", 0)
    hass.states.set(E["weather"], "cloudy", {"temperature": 15})
    _hall_temp(hass, 12)
    # Comfort target (19.5) -> 7.5 °C deficit -> capped at 120. ECO target is
    # the eco-low slider (14) -> 2 °C deficit + 2.5 finish at 3 °C/h -> 90 min.
    assert ctrl._zone_preheat_minutes(ZA) == 120
    assert ctrl._zone_preheat_minutes(ZA, eco=True) == 90


def test_far_off_booking_adds_predicted_cooling():
    ctrl, hass = make_controller()
    _set_rate(ctrl, "hall_comfort_temp", 22)
    _set_rate(ctrl, "zone_a_warmup_rate", 10)
    _set_rate(ctrl, "zone_a_heatloss_pct", 5)
    hass.states.set(E["weather"], "cloudy", {"temperature": 15})
    _hall_temp(hass, 19)
    # Event now: 3 °C deficit at 6 °C/h gross less a 0.275 °C/h leak. Event 6 h
    # away: the room will keep cooling until the pre-heat starts, so the lead grows.
    near = ctrl._zone_preheat_minutes(ZA, gap_hours=0)
    far = ctrl._zone_preheat_minutes(ZA, gap_hours=6)
    assert near == round(60 * 5.5 / (6 - 0.05 * 5.5))
    assert far > near


def test_completed_preheat_updates_the_learned_gain():
    ctrl, hass = make_controller()
    _set_rate(ctrl, "zone_a_warmup_rate", 20)
    _set_rate(ctrl, "zone_a_heatloss_pct", 0)  # isolate the gain from the leak
    _set_rate(ctrl, "hall_comfort_temp", 22)  # target, so the climb is a 4 °C sample
    _hall_temp(hass, 18)
    hass.states.set(E["weather"], "cloudy", {"temperature": 5})  # cold: learning gated to it
    _start_preheat(ctrl)
    ctrl._update_warmup_learning()  # sample starts at 18 °C
    assert ctrl._warmup_start[ZA] is not None
    for t in (19, 20, 21, 22):
        advance(ctrl, 30)
        _hall_temp(hass, t)
        ctrl._update_warmup_learning()
    assert ctrl._warmup_start[ZA] is None
    # 4 °C + 2.5 finish over 2 h = 3.25 °C/h -> 18.46 min/°C; 20 + 0.3 * (18.46 - 20).
    assert ctrl.number("zone_a_warmup_rate") == pytest.approx(19.54, abs=0.01)


def test_a_boost_or_occupancy_climb_is_not_sampled():
    # Only a real pre-heat teaches: a Boost never throttles (reads ~7, out of
    # family) and an occupancy climb carries the people's own heat.
    for reason in ("boost", "motion", "booking"):
        ctrl, hass = make_controller()
        _hall_temp(hass, 18)
        hass.states.set(E["weather"], "cloudy", {"temperature": 5})
        ctrl.applied[ZA] = PRESET_COMFORT
        ctrl._preset_reason[ZA] = reason
        ctrl._update_warmup_learning()
        assert ctrl._warmup_start[ZA] is None, reason


def test_no_sample_opens_inside_the_startup_grace():
    # A restart mid-pre-heat re-applies comfort with reason `preheat` on a room
    # that is already heating: a fresh sample there would read fast. Lose it.
    ctrl, hass = make_controller()
    ctrl._started_at = ctrl._now() - timedelta(minutes=DRIVE_STARTUP_GRACE_MINUTES - 1)
    _hall_temp(hass, 18)
    _start_preheat(ctrl)
    ctrl._update_warmup_learning()
    assert ctrl._warmup_start[ZA] is None


def test_aborted_warmup_is_not_folded():
    ctrl, hass = make_controller()
    _set_rate(ctrl, "zone_a_warmup_rate", 20)
    _set_rate(ctrl, "hall_comfort_temp", 22)
    _hall_temp(hass, 18)
    hass.states.set(E["weather"], "cloudy", {"temperature": 5})
    _start_preheat(ctrl)
    ctrl._update_warmup_learning()
    # A big, fast rise — but the preset leaves comfort before target: a
    # truncated climb has not paid the finish and would read fast.
    advance(ctrl, 60)
    _hall_temp(hass, 21)
    ctrl.applied[ZA] = PRESET_ECO
    ctrl._update_warmup_learning()
    assert ctrl._warmup_start[ZA] is None
    assert ctrl.number("zone_a_warmup_rate") == 20  # unchanged
    (evt,) = [e for e in ctrl.audit.to_list() if e.get("event") == "warmup_sample"]
    assert evt["reached_target"] is False and evt["accepted"] is False


def test_the_sample_is_timed_on_the_coldest_probe():
    # The lead is sized on the coldest end and the shortfall judges it, so the
    # climb is timed on it too: the warm end arriving does not close the sample.
    hb, hf = E["hall"]
    ctrl, hass = make_controller()
    _set_rate(ctrl, "hall_comfort_temp", 22)
    hass.states.set(E["weather"], "cloudy", {"temperature": 5})
    hass.states.set(hb, "heat", {"current_temperature": 18})
    hass.states.set(hf, "heat", {"current_temperature": 18})
    _start_preheat(ctrl)
    ctrl._update_warmup_learning()
    advance(ctrl, 60)
    hass.states.set(hb, "heat", {"current_temperature": 22})  # warm end arrived
    hass.states.set(hf, "heat", {"current_temperature": 20})  # cold end has not
    ctrl._update_warmup_learning()
    assert ctrl._warmup_start[ZA] is not None  # still open
    advance(ctrl, 30)
    hass.states.set(hf, "heat", {"current_temperature": 22})
    ctrl._update_warmup_learning()
    assert ctrl._warmup_start[ZA] is None
    (evt,) = [e for e in ctrl.audit.to_list() if e.get("event") == "warmup_sample"]
    assert evt["minutes"] == pytest.approx(90, abs=0.1)


def test_a_probe_dropping_out_cannot_close_the_sample_early():
    # With the cold end unavailable the next-coldest probe would read "arrived";
    # the close needs as many readable probes as the open had.
    hb, hf = E["hall"]
    ctrl, hass = make_controller()
    _set_rate(ctrl, "hall_comfort_temp", 22)
    hass.states.set(E["weather"], "cloudy", {"temperature": 5})
    hass.states.set(hb, "heat", {"current_temperature": 18})
    hass.states.set(hf, "heat", {"current_temperature": 18})
    _start_preheat(ctrl)
    ctrl._update_warmup_learning()
    advance(ctrl, 60)
    hass.states.set(hb, "heat", {"current_temperature": 22})
    hass.states.set(hf, "unavailable", {})
    ctrl._update_warmup_learning()
    assert ctrl._warmup_start[ZA] is not None  # held open, not closed on hb alone


def test_cooloff_learning_is_gap_normalised():
    from scout_testkit import PRESET_ICE

    ctrl, hass = make_controller()
    _set_rate(ctrl, "zone_a_heatloss_pct", 20)
    hass.states.set(E["weather"], "cloudy", {"temperature": 10})
    _hall_temp(hass, 20)
    ctrl.applied[ZA] = PRESET_ICE
    _begin_cooloff(ctrl)  # sample anchors at 20 °C (after the settle delay)
    assert ctrl._cooloff_start[ZA] is not None
    # Cool smoothly in 0.6 °C steps: each single tick stays well under the
    # 1.5 °C step guard, so this reads as fabric loss, not a discontinuity.
    advance(ctrl, 60)
    _hall_temp(hass, 19.4)
    ctrl._update_cooloff_learning()  # 0.6 °C so far: below the fold trigger
    advance(ctrl, 60)
    _hall_temp(hass, 18.8)  # 1.2 °C over 2 h at an average gap of 9.4 -> k=0.064/h
    ctrl._update_cooloff_learning()
    # 20 %/h + 0.3 * (6.38 - 20) = 15.91 %/h
    assert ctrl.number("zone_a_heatloss_pct") == pytest.approx(15.91, abs=0.01)


def test_cooloff_single_tick_step_is_not_learned():
    """An open window with no contact sensor (the office has none) or a Rointe
    probe unfreezing dumps the whole drop into one tick. That discontinuity is
    rejected rather than learned as fabric loss — 2026-07-22 exactly this
    corrupted the office EWMA from ~4.7 to ~24 %/h.
    """
    from scout_testkit import PRESET_ICE

    ctrl, hass = make_controller()
    _set_rate(ctrl, "zone_a_heatloss_pct", 10)
    hass.states.set(E["weather"], "cloudy", {"temperature": 12})
    _hall_temp(hass, 22)
    ctrl.applied[ZA] = PRESET_ICE
    _begin_cooloff(ctrl)  # anchor 22 °C (after the settle delay)
    advance(ctrl, 60)
    _hall_temp(hass, 19)  # 3 °C in a single tick over a real 8.5 °C gap
    ctrl._update_cooloff_learning()
    sample = [e for e in ctrl.audit.to_list() if e.get("event") == "cooloff_sample"][-1]
    # Drop, duration and gap all pass their floors — only the step guard stops it.
    assert sample["max_tick_drop"] == pytest.approx(3.0)
    assert sample["accepted"] is False
    assert ctrl.number("zone_a_heatloss_pct") == 10  # unchanged, not corrupted


def test_solar_gain_does_not_teach_insulation():
    from scout_testkit import PRESET_ICE

    ctrl, hass = make_controller()
    _set_rate(ctrl, "zone_a_heatloss_pct", 20)
    hass.states.set(E["weather"], "cloudy", {"temperature": 15})
    _hall_temp(hass, 20)
    ctrl.applied[ZA] = PRESET_ICE
    ctrl._update_cooloff_learning()
    advance(ctrl, 240)
    _hall_temp(hass, 24)  # July roof: the room GAINED heat while unheated
    ctrl._update_cooloff_learning()
    assert ctrl.number("zone_a_heatloss_pct") == 20  # unchanged, re-anchored
    assert ctrl._cooloff_start[ZA][1] == 24


def test_office_comfort_target_is_cached_from_its_heater():
    # With driving OFF the office setpoint is device-managed, so the target is
    # the last value cached from the heater while it was in comfort.
    ctrl, hass = make_controller()
    ctrl._switches["drive_to_target"].is_on = False
    hass.states.set(E["office"][0], "heat", {"current_temperature": 20, "temperature": 21})
    ctrl.applied[ZB] = PRESET_COMFORT
    ctrl._update_warmup_learning()
    assert ctrl._zone_target(ZB) == 21


def test_office_target_is_the_slider_when_driving():
    # With driving ON the integration owns the office comfort setpoint, so the
    # target is the office_comfort_temp slider regardless of the cached value.
    ctrl, hass = make_controller()
    hass.states.set(E["office"][0], "heat", {"current_temperature": 20, "temperature": 21})
    ctrl.applied[ZB] = PRESET_COMFORT
    ctrl._update_warmup_learning()
    assert ctrl._zone_target(ZB) == ctrl.number("office_comfort_temp")


def test_office_target_defaults_to_hall_slider_until_seen():
    ctrl, _ = make_controller()
    assert ctrl._zone_target(ZB) == ctrl.number("hall_comfort_temp")


def test_updated_cooling_k_thresholds():
    from custom_components.scout_hut_heating.preheat import updated_cooling_k

    assert updated_cooling_k(0.2, 4, 0.5, 8) == 0.2  # drop too small
    assert updated_cooling_k(0.2, 0.2, 2, 8) == 0.2  # duration too short
    assert updated_cooling_k(0.2, 2, 2, 2) == 0.2  # gap too small to normalise
    assert updated_cooling_k(0.2, 2, 2, 10) == pytest.approx(0.17)  # observed 0.1/h


def test_out_of_family_sample_is_rejected_whole():
    # An insulated office at a learned 5 %/h can't suddenly lose 33 %/h — that's an
    # open window, not the fabric. The sample must be rejected, k left untouched.
    from custom_components.scout_hut_heating.preheat import (
        cooling_sample_is_outlier,
        cooling_observed_k,
        updated_cooling_k,
    )

    # drop 1.0 over 0.5 h at gap 6 -> observed 0.333 (33 %/h), 6.7x the 0.05 baseline.
    assert cooling_observed_k(0.5, 1.0, 6.0) == pytest.approx(0.3333, abs=1e-3)
    assert cooling_sample_is_outlier(0.05, 0.3333) is True
    assert updated_cooling_k(0.05, 0.5, 1.0, 6.0) == 0.05  # rejected, unchanged

    # A genuine winter doubling (~2x) is NOT an outlier and still folds in.
    assert cooling_sample_is_outlier(0.05, 0.10) is False
    # At the coarse seed the MAX_COOL_K clamp keeps observed below the ratio, so
    # the guard can never fire before a real low baseline exists.
    assert cooling_sample_is_outlier(0.25, 0.5) is False


def test_single_sample_cannot_yank_the_baseline():
    # In-family but large: one sample may only nudge the EWMA a bounded fraction,
    # never leap it. k 0.10, observed 0.25 (2.5x, under the 3x outlier line) would
    # fold to 0.145 unclamped; the 25 % step cap holds it to 0.125.
    from custom_components.scout_hut_heating.preheat import updated_cooling_k

    assert updated_cooling_k(0.10, 0.5, 1.25, 10.0) == pytest.approx(0.125)


def test_cooling_prediction_never_goes_below_the_frost_floor():
    # A booking days away in freezing weather must not predict the room below
    # the anti-frost 7 °C the heating holds even when "off".
    a = required_lead_minutes(
        rate=20, indoor=19, target=22, outdoor=-5, max_minutes=600,
        gap_hours=24, cool_k=0.25,
    )
    b = required_lead_minutes(
        rate=20, indoor=19, target=22, outdoor=-5, max_minutes=600,
        gap_hours=240, cool_k=0.25,
    )
    assert a == b  # both clamped at the (target - 7 °C) deficit


def test_cooling_prediction_decays_toward_outdoor_not_below():
    # Newton cooling: a mild night can never predict the room below the
    # OUTDOOR temperature, however long the gap — the July failure mode of
    # the old constant-rate model. Deficit 7 + finish, less the leak at the
    # mid-climb gap ((15 + 22)/2 - 15 = 3.5).
    lead = required_lead_minutes(
        rate=20, indoor=19, target=22, outdoor=15, max_minutes=600,
        gap_hours=1000, cool_k=0.25,
    )
    assert lead == pytest.approx(60 * (7 + APPROACH_TAIL_C) / (3 - 0.25 * 3.5))


# --- Lateral spread: coldest-reading pre-heat + the spread diagnostic -----------

def _both_hall_temps(hass, a, b):
    hass.states.set(E["hall"][0], "heat", {"current_temperature": a})
    hass.states.set(E["hall"][1], "heat", {"current_temperature": b})


def test_preheat_sizes_for_the_coldest_end_of_the_hall():
    ctrl, hass = make_controller()
    _set_rate(ctrl, "hall_comfort_temp", 22)  # pin: tests the maths, not the default
    _set_rate(ctrl, "zone_a_warmup_rate", 20)
    _set_rate(ctrl, "zone_a_heatloss_pct", 0)
    _set_rate(ctrl, "preheat_minutes", 240)
    hass.states.set(E["weather"], "cloudy", {"temperature": 15})
    _both_hall_temps(hass, 21, 18)  # warm end must not cut the lead short
    # Coldest reading 18 -> 4 °C deficit + finish at 3 °C/h -> 130 min (the
    # average, 19.5, would give 100).
    assert ctrl._zone_preheat_minutes(ZA) == 130


def test_hall_temp_spread_diagnostic():
    ctrl, hass = make_controller()
    _both_hall_temps(hass, 21, 18)
    assert ctrl.hall_temp_spread == 3.0


def test_hall_temp_spread_needs_two_readings():
    ctrl, hass = make_controller()
    _hall_temp(hass, 20)  # only one heater reporting
    assert ctrl.hall_temp_spread is None


# --- Audit-fix regressions -------------------------------------------------------

def test_warmup_rate_ignores_implausibly_fast_samples():
    # A cloud-lagged reading catching up in one jump is not a real warm-up.
    assert updated_rate(20, 5, 4) == 20


def test_clamped_cooling_prediction_uses_the_full_floor_deficit():
    # Once the 7 °C floor binds, the room bottomed out long before the
    # pre-heat begins: deficit 15 + finish over the gain less the leak at the
    # mid-climb gap ((7 + 22)/2 + 5 = 19.5).
    lead = required_lead_minutes(
        rate=8, indoor=19, target=22, outdoor=-5, max_minutes=600,
        gap_hours=48, cool_k=0.25,
    )
    assert lead == pytest.approx(60 * (15 + APPROACH_TAIL_C) / (7.5 - 0.25 * 19.5))


def test_fast_heat_loss_is_learnable():
    from scout_testkit import PRESET_ICE

    ctrl, hass = make_controller()
    _set_rate(ctrl, "zone_a_heatloss_pct", 20)
    hass.states.set(E["weather"], "cloudy", {"temperature": 9})
    _hall_temp(hass, 20)
    ctrl.applied[ZA] = PRESET_ICE
    _begin_cooloff(ctrl)  # anchor 20 °C (after the settle delay)
    advance(ctrl, 20)
    _hall_temp(hass, 19)  # 1 °C in 20 min: too short a sample to fold...
    ctrl._update_cooloff_learning()
    assert ctrl.number("zone_a_heatloss_pct") == 20
    assert ctrl._cooloff_start[ZA][1] == 20  # ...and the anchor must NOT roll
    advance(ctrl, 20)
    _hall_temp(hass, 18)  # 2 °C over 40 min at an average gap of 10 -> 0.3/h
    ctrl._update_cooloff_learning()
    # 20 %/h + 0.3 * (30 - 20) = 23 %/h
    assert ctrl.number("zone_a_heatloss_pct") == pytest.approx(23, abs=0.01)


def test_open_door_does_not_teach_heat_loss():
    from scout_testkit import PRESET_ICE

    ctrl, hass = make_controller()
    _set_rate(ctrl, "zone_a_heatloss_pct", 20)
    hass.states.set(E["weather"], "cloudy", {"temperature": 5})
    _hall_temp(hass, 20)
    ctrl.applied[ZA] = PRESET_ICE
    ctrl._update_cooloff_learning()
    ctrl.opening_ice[ZA] = True  # door propped open: ventilation, not fabric
    advance(ctrl, 120)
    _hall_temp(hass, 14)
    ctrl._update_cooloff_learning()
    assert ctrl._cooloff_start[ZA] is None  # sample discarded
    assert ctrl.number("zone_a_heatloss_pct") == 20


def test_prediction_falls_back_until_the_fans_rate_is_trained():
    from custom_components.scout_hut_heating.const import CONF_FAN_MASTER

    ctrl, hass = make_controller(config_overrides={CONF_FAN_MASTER: "switch.fan_master"})
    _set_rate(ctrl, "hall_comfort_temp", 22)
    _set_rate(ctrl, "zone_a_warmup_rate", 20)
    _set_rate(ctrl, "zone_a_heatloss_pct", 0)
    hass.states.set(E["weather"], "cloudy", {"temperature": 15})
    _hall_temp(hass, 19)
    # Fans-rate still at its fail-safe seed (60): predict with the base rate.
    assert ctrl._zone_preheat_minutes(ZA) == 110
    _set_rate(ctrl, "zone_a_warmup_rate_fans", 15)  # now trained: 4 °C/h gross
    assert ctrl._zone_preheat_minutes(ZA) == round(60 * 5.5 / 4)


def test_learning_lands_in_the_key_the_lead_predicts_with():
    # A cold pre-heat whose fans ran only part of the climb used to fold into
    # the base key the lead never reads (2026-09-16). Learn where you predict.
    from custom_components.scout_hut_heating.const import CONF_FAN_MASTER

    ctrl, hass = make_controller(config_overrides={CONF_FAN_MASTER: "switch.fan_master"})
    hass.states.set("switch.fan_master", "off")  # fans never actually ran
    _set_rate(ctrl, "zone_a_warmup_rate_fans", 20)
    _set_rate(ctrl, "zone_a_heatloss_pct", 0)
    _set_rate(ctrl, "hall_comfort_temp", 22)
    _hall_temp(hass, 18)
    hass.states.set(E["weather"], "cloudy", {"temperature": 5})
    _start_preheat(ctrl)
    ctrl._update_warmup_learning()
    for t in (19, 20, 21, 22):
        advance(ctrl, 30)
        _hall_temp(hass, t)
        ctrl._update_warmup_learning()
    (evt,) = [e for e in ctrl.audit.to_list() if e.get("event") == "warmup_sample"]
    assert evt["rate_key"] == "zone_a_warmup_rate_fans"
    assert evt["fan_ticks"] == 0
    assert ctrl.number("zone_a_warmup_rate_fans") == pytest.approx(19.54, abs=0.01)


def test_all_day_event_start_parses_to_an_aware_midnight():
    from custom_components.scout_hut_heating.coordinator import ScoutController

    parsed = ScoutController._parse_event_start("2026-07-11")
    assert parsed is not None and parsed.tzinfo is not None


def test_calendar_blip_keeps_the_previous_window(monkeypatch):
    from scout_testkit import preheat_window

    ctrl, _ = make_controller()
    preheat_window(ctrl, ZA)

    async def _err(cal, minutes):
        return None  # calendar service unavailable

    monkeypatch.setattr(ctrl, "_async_calendar_events", _err)
    from scout_testkit import run

    run(ctrl._async_refresh_calendars())
    assert ctrl.cal_window[ZA] is True  # window survives the blip


def _events_for(hall_events):
    """Fake `_async_calendar_events` returning `hall_events` for the hall only."""

    async def fake(cal, minutes):
        return list(hall_events) if cal == E["cal_hall"] else []

    return fake


def test_preheat_window_latches_open_as_the_room_warms(monkeypatch):
    """Once open FOR AN EVENT, the window holds until it starts — it must not
    re-close when the warming room shrinks the recomputed lead below the gap."""
    from homeassistant.util import dt as dt_util
    from scout_testkit import run

    ctrl, hass = make_controller()
    _set_rate(ctrl, "zone_a_heatloss_pct", 0)  # no cooling prediction
    start_iso = (dt_util.now() + timedelta(minutes=25)).isoformat()
    monkeypatch.setattr(
        ctrl, "_async_calendar_events", _events_for([{"start": start_iso, "summary": "squirrels"}])
    )

    # Cold room: the lead is long, so the window opens (and latches for this event).
    _hall_temp(hass, 12.0)
    run(ctrl._async_refresh_calendars())
    assert ctrl.cal_window[ZA] is True

    # Room now warm: the recomputed lead shrinks below the gap, but the latch
    # (keyed to this event) holds the window open.
    _hall_temp(hass, 22.0)
    run(ctrl._async_refresh_calendars())
    assert ctrl.cal_window[ZA] is True


def test_preheat_latch_survives_a_restart(monkeypatch):
    """A restart mid-pre-heat must not re-derive the window on a bare
    `gap <= lead`: the 2026-09-17 deploy restart did, the re-derivation ran
    off a panel-inflated reading, closed the window and the booking arrived
    +1.0 short. The latch is persisted with the window it belongs to."""
    from homeassistant.util import dt as dt_util
    from scout_testkit import run

    ctrl, hass = make_controller()
    _set_rate(ctrl, "zone_a_heatloss_pct", 0)
    start = dt_util.now() + timedelta(minutes=25)
    events = _events_for([{"start": start.isoformat(), "summary": "squirrels"}])
    monkeypatch.setattr(ctrl, "_async_calendar_events", events)
    _hall_temp(hass, 12.0)
    run(ctrl._async_refresh_calendars())
    assert ctrl.cal_window[ZA] is True
    snapshot = ctrl._state_snapshot()
    assert snapshot["preheat_open_for"][ZA] == start.isoformat()

    # Restart: the room reads warm (the just-heated panels), so a bare
    # gap-vs-lead test would close the window. The restored latch holds it.
    ctrl2, hass2 = make_controller()

    async def _load():
        return snapshot

    ctrl2._store.async_load = _load
    run(ctrl2._async_restore_state())
    assert ctrl2._preheat_open_for[ZA] == start
    monkeypatch.setattr(ctrl2, "_async_calendar_events", events)
    _set_rate(ctrl2, "zone_a_heatloss_pct", 0)
    _hall_temp(hass2, 22.0)
    run(ctrl2._async_refresh_calendars())
    assert ctrl2.cal_window[ZA] is True

    # Without the latch the same refresh closes the window — the pre-fix path.
    ctrl3, hass3 = make_controller()
    monkeypatch.setattr(ctrl3, "_async_calendar_events", events)
    _set_rate(ctrl3, "zone_a_heatloss_pct", 0)
    _hall_temp(hass3, 22.0)
    run(ctrl3._async_refresh_calendars())
    assert ctrl3.cal_window[ZA] is False


def test_a_stale_preheat_latch_is_not_restored():
    """A latch for an event that started while HA was down is dropped: the
    next refresh judges whatever comes next fresh."""
    from scout_testkit import run

    ctrl, _ = make_controller()
    ctrl._preheat_open_for[ZA] = ctrl._now() - timedelta(minutes=5)
    snapshot = ctrl._state_snapshot()
    ctrl2, _ = make_controller()

    async def _load():
        return snapshot

    ctrl2._store.async_load = _load
    run(ctrl2._async_restore_state())
    assert ctrl2._preheat_open_for[ZA] is None


def test_preheat_latch_does_not_bridge_into_the_next_booking(monkeypatch):
    """Back-to-back bookings inside the look-ahead must NOT hold comfort across
    the empty gap: when booking A ends, booking B's pre-heat is judged fresh."""
    from homeassistant.util import dt as dt_util
    from scout_testkit import E, run

    ctrl, hass = make_controller()
    _hall_temp(hass, 22.0)  # warm -> B needs only a short lead
    _set_rate(ctrl, "zone_a_heatloss_pct", 0)
    cal = E["cal_hall"]

    # Booking A is running now; booking B starts in 100 min (inside the 120 cap).
    b_start = (dt_util.now() + timedelta(minutes=100)).isoformat()
    monkeypatch.setattr(
        ctrl, "_async_calendar_events", _events_for([{"start": b_start, "summary": "beavers"}])
    )
    hass.states.set(cal, "on", {"message": "squirrels"})  # A running
    run(ctrl._async_refresh_calendars())
    assert ctrl.cal_window[ZA] is True  # A running -> window open

    # A ends. B is 100 min out; a warm room's lead is ~15 min, so the window must
    # DROP (not stay latched from A) — the hall can rest at eco across the gap.
    hass.states.set(cal, "off")
    run(ctrl._async_refresh_calendars())
    assert ctrl.cal_window[ZA] is False


def test_latched_window_still_releases_when_the_event_goes_away(monkeypatch):
    """The latch is not a trap: an ended/cancelled event (empty look-ahead)
    still closes the window."""
    from scout_testkit import run

    ctrl, _ = make_controller()
    ctrl.cal_window[ZA] = True  # a window was open
    monkeypatch.setattr(ctrl, "_async_calendar_events", _events_for([]))  # nothing ahead
    run(ctrl._async_refresh_calendars())
    assert ctrl.cal_window[ZA] is False
