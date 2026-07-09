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


def test_zone_lead_uses_room_and_weather(monkeypatch):
    ctrl, hass = make_controller()
    _hall_temp(hass, 19)
    hass.states.set(E["weather"], "cloudy", {"temperature": 15})
    assert ctrl._zone_preheat_minutes(ZA) == 60


def test_zone_lead_without_room_reading_is_the_cap():
    ctrl, _ = make_controller()
    assert ctrl._zone_preheat_minutes(ZA) == 120


def test_completed_warmup_updates_the_learned_rate():
    ctrl, hass = make_controller()
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
    _hall_temp(hass, 18)
    ctrl.applied[ZA] = PRESET_COMFORT
    ctrl._update_warmup_learning()
    advance(ctrl, 20)
    _hall_temp(hass, 18.4)
    ctrl.applied[ZA] = PRESET_ECO  # warm-up ended early (booking over)
    ctrl._update_warmup_learning()
    assert ctrl._warmup_start[ZA] is None
    assert ctrl.number("zone_a_warmup_rate") == 20  # unchanged


def test_office_comfort_target_is_cached_from_its_heater():
    ctrl, hass = make_controller()
    hass.states.set(E["office"][0], "heat", {"current_temperature": 20, "temperature": 21})
    ctrl.applied[ZB] = PRESET_COMFORT
    ctrl._update_warmup_learning()
    assert ctrl._zone_target(ZB) == 21


def test_office_target_defaults_to_hall_slider_until_seen():
    ctrl, _ = make_controller()
    assert ctrl._zone_target(ZB) == ctrl.number("hall_comfort_temp")
