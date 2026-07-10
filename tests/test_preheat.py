"""Adaptive pre-heat (optimum start): pure model + coordinator wiring."""

import pytest

from custom_components.scout_hut_heating.preheat import (
    MIN_LEAD,
    required_lead_minutes,
    updated_rate,
)
from scout_testkit import PRESET_COMFORT, PRESET_ECO, ZA, ZB, advance, make_controller, E


# --- Pure model ----------------------------------------------------------------

def test_lead_scales_with_deficit():
    # 3 °C short at 20 min/°C, mild outside: 60 minutes.
    assert required_lead_minutes(
        rate=20, indoor=19, target=22, outdoor=15, max_minutes=120
    ) == 60


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


def test_cold_weather_adds_margin():
    # 5 °C outside: 10 °C below base -> +10% on the 60-minute lead.
    assert required_lead_minutes(
        rate=20, indoor=19, target=22, outdoor=5, max_minutes=120
    ) == 66


def test_rate_update_moves_toward_the_observation():
    # Observed 30 min/°C vs learned 20: EWMA (alpha 0.3) -> 23.
    assert updated_rate(20, 120, 4) == 23


def test_rate_update_ignores_tiny_rises():
    assert updated_rate(20, 30, 0.4) == 20


def test_rate_update_clamps_wild_observations():
    # 300 min for 1 °C would be 300 min/°C; clamped observation (60) pulls
    # the estimate up by at most alpha * (60 - 20).
    assert updated_rate(20, 300, 1) == 32


# --- Coordinator wiring ----------------------------------------------------------

def _hall_temp(hass, temp):
    hass.states.set(E["hall"][0], "heat", {"current_temperature": temp})


def _set_rate(ctrl, key, value):
    ctrl._numbers[key].native_value = value


def test_zone_lead_uses_room_and_weather():
    ctrl, hass = make_controller()
    _set_rate(ctrl, "zone_a_warmup_rate", 20)
    _set_rate(ctrl, "zone_a_cooloff_rate", 0)  # isolate the deficit term
    _hall_temp(hass, 19)
    hass.states.set(E["weather"], "cloudy", {"temperature": 15})
    assert ctrl._zone_preheat_minutes(ZA) == 60


def test_unlearned_zone_uses_the_full_cap():
    # The rates are seeded at the slowest plausible value, so before any
    # learning a normal cold start computes past the cap and clamps to it —
    # fail-safe warm, exactly the old fixed behaviour.
    ctrl, hass = make_controller()
    _hall_temp(hass, 18)  # 4 °C deficit x 60 min/°C = 240 -> cap
    assert ctrl._zone_preheat_minutes(ZA) == 120


def test_zone_lead_without_room_reading_is_the_cap():
    ctrl, _ = make_controller()
    assert ctrl._zone_preheat_minutes(ZA) == 120


def test_eco_booking_aims_at_the_eco_low_target():
    ctrl, hass = make_controller()
    _set_rate(ctrl, "zone_a_warmup_rate", 20)
    _set_rate(ctrl, "zone_a_cooloff_rate", 0)
    hass.states.set(E["weather"], "cloudy", {"temperature": 15})
    _hall_temp(hass, 12)
    # Comfort target 22 -> 10 °C deficit -> capped at 120. ECO target is the
    # eco-low slider (14) -> 2 °C deficit -> 40 minutes.
    assert ctrl._zone_preheat_minutes(ZA) == 120
    assert ctrl._zone_preheat_minutes(ZA, eco=True) == 40


def test_far_off_booking_adds_predicted_cooling():
    ctrl, hass = make_controller()
    _set_rate(ctrl, "zone_a_warmup_rate", 20)
    _set_rate(ctrl, "zone_a_cooloff_rate", 1.0)
    hass.states.set(E["weather"], "cloudy", {"temperature": 15})
    _hall_temp(hass, 19)
    # Event now: 3 °C deficit -> 60 min. Event 6 h away: the room will keep
    # cooling until the pre-heat starts, so the lead grows.
    near = ctrl._zone_preheat_minutes(ZA, gap_hours=0)
    far = ctrl._zone_preheat_minutes(ZA, gap_hours=6)
    assert near == 60
    assert far > near


def test_completed_warmup_updates_the_learned_rate():
    ctrl, hass = make_controller()
    _set_rate(ctrl, "zone_a_warmup_rate", 20)
    _hall_temp(hass, 18)
    ctrl.applied[ZA] = PRESET_COMFORT
    ctrl._update_warmup_learning()  # sample starts at 18 °C
    assert ctrl._warmup_start[ZA] is not None
    advance(ctrl, 120)
    _hall_temp(hass, 22)  # target (22) reached after 120 min / 4 °C
    ctrl._update_warmup_learning()
    assert ctrl._warmup_start[ZA] is None
    assert ctrl.number("zone_a_warmup_rate") == pytest.approx(23, abs=0.01)  # 20 + 0.3 * (30 - 20)


def test_aborted_warmup_with_small_rise_is_ignored():
    ctrl, hass = make_controller()
    _set_rate(ctrl, "zone_a_warmup_rate", 20)
    _hall_temp(hass, 18)
    ctrl.applied[ZA] = PRESET_COMFORT
    ctrl._update_warmup_learning()
    advance(ctrl, 20)
    _hall_temp(hass, 18.4)
    ctrl.applied[ZA] = PRESET_ECO  # warm-up ended early (booking over)
    ctrl._update_warmup_learning()
    assert ctrl._warmup_start[ZA] is None
    assert ctrl.number("zone_a_warmup_rate") == 20  # unchanged


def test_cooloff_learning_from_an_unheated_zone():
    from scout_testkit import PRESET_ICE

    ctrl, hass = make_controller()
    _set_rate(ctrl, "zone_a_cooloff_rate", 2.0)
    _hall_temp(hass, 20)
    ctrl.applied[ZA] = PRESET_ICE
    ctrl._update_cooloff_learning()  # sample anchors at 20 °C
    assert ctrl._cooloff_start[ZA] is not None
    advance(ctrl, 240)  # four hours later...
    _hall_temp(hass, 16)  # ...4 °C lost -> observed 1 °C/h
    ctrl._update_cooloff_learning()
    assert ctrl.number("zone_a_cooloff_rate") == pytest.approx(1.7, abs=0.01)  # 2 + 0.3*(1-2)


def test_solar_gain_does_not_teach_insulation():
    from scout_testkit import PRESET_ICE

    ctrl, hass = make_controller()
    _set_rate(ctrl, "zone_a_cooloff_rate", 2.0)
    _hall_temp(hass, 20)
    ctrl.applied[ZA] = PRESET_ICE
    ctrl._update_cooloff_learning()
    advance(ctrl, 240)
    _hall_temp(hass, 24)  # July roof: the room GAINED heat while unheated
    ctrl._update_cooloff_learning()
    assert ctrl.number("zone_a_cooloff_rate") == 2.0  # unchanged, re-anchored
    assert ctrl._cooloff_start[ZA][1] == 24


def test_office_comfort_target_is_cached_from_its_heater():
    ctrl, hass = make_controller()
    hass.states.set(E["office"][0], "heat", {"current_temperature": 20, "temperature": 21})
    ctrl.applied[ZB] = PRESET_COMFORT
    ctrl._update_warmup_learning()
    assert ctrl._zone_target(ZB) == 21


def test_office_target_defaults_to_hall_slider_until_seen():
    ctrl, _ = make_controller()
    assert ctrl._zone_target(ZB) == ctrl.number("hall_comfort_temp")


def test_updated_cooling_rate_thresholds():
    from custom_components.scout_hut_heating.preheat import updated_cooling_rate

    assert updated_cooling_rate(2.0, 4, 0.5) == 2.0  # drop too small
    assert updated_cooling_rate(2.0, 0.2, 2) == 2.0  # duration too short
    assert updated_cooling_rate(2.0, 2, 2) == pytest.approx(1.7)  # observed 1 °C/h


def test_cooling_prediction_never_goes_below_the_frost_floor():
    # A booking days away must not predict the room below the anti-frost 7 °C.
    a = required_lead_minutes(
        rate=20, indoor=19, target=22, outdoor=15, max_minutes=600,
        gap_hours=24, cool_rate=2.0,
    )
    b = required_lead_minutes(
        rate=20, indoor=19, target=22, outdoor=15, max_minutes=600,
        gap_hours=240, cool_rate=2.0,
    )
    assert a == b  # both clamped at the (target - 7 °C) deficit


# --- Lateral spread: coldest-reading pre-heat + the spread diagnostic -----------

def _both_hall_temps(hass, a, b):
    hass.states.set(E["hall"][0], "heat", {"current_temperature": a})
    hass.states.set(E["hall"][1], "heat", {"current_temperature": b})


def test_preheat_sizes_for_the_coldest_end_of_the_hall():
    ctrl, hass = make_controller()
    _set_rate(ctrl, "zone_a_warmup_rate", 20)
    _set_rate(ctrl, "zone_a_cooloff_rate", 0)
    hass.states.set(E["weather"], "cloudy", {"temperature": 15})
    _both_hall_temps(hass, 21, 18)  # warm end must not cut the lead short
    # Coldest reading 18 -> 4 °C deficit -> 80 min (average would give 50).
    assert ctrl._zone_preheat_minutes(ZA) == 80


def test_hall_temp_spread_diagnostic():
    ctrl, hass = make_controller()
    _both_hall_temps(hass, 21, 18)
    assert ctrl.hall_temp_spread == 3.0


def test_hall_temp_spread_needs_two_readings():
    ctrl, hass = make_controller()
    _hall_temp(hass, 20)  # only one heater reporting
    assert ctrl.hall_temp_spread is None
