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
    _set_rate(ctrl, "hall_comfort_temp", 22)  # pin: tests the maths, not the default
    _set_rate(ctrl, "zone_a_warmup_rate", 20)
    _set_rate(ctrl, "zone_a_heatloss_pct", 0)  # isolate the deficit term
    _hall_temp(hass, 19)
    hass.states.set(E["weather"], "cloudy", {"temperature": 15})
    assert ctrl._zone_preheat_minutes(ZA) == 60


def test_unlearned_zone_uses_the_full_cap():
    # The rates are seeded at the slowest plausible value, so before any
    # learning a normal cold start computes past the cap and clamps to it —
    # fail-safe warm, exactly the old fixed behaviour.
    ctrl, hass = make_controller()
    _hall_temp(hass, 17)  # 2.5 °C deficit x 60 min/°C = 150 -> cap
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
    # the eco-low slider (14) -> 2 °C deficit -> 40 minutes.
    assert ctrl._zone_preheat_minutes(ZA) == 120
    assert ctrl._zone_preheat_minutes(ZA, eco=True) == 40


def test_far_off_booking_adds_predicted_cooling():
    ctrl, hass = make_controller()
    _set_rate(ctrl, "hall_comfort_temp", 22)
    _set_rate(ctrl, "zone_a_warmup_rate", 20)
    _set_rate(ctrl, "zone_a_heatloss_pct", 25)
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


def test_cooloff_learning_is_gap_normalised():
    from scout_testkit import PRESET_ICE

    ctrl, hass = make_controller()
    _set_rate(ctrl, "zone_a_heatloss_pct", 20)
    hass.states.set(E["weather"], "cloudy", {"temperature": 10})
    _hall_temp(hass, 20)
    ctrl.applied[ZA] = PRESET_ICE
    ctrl._update_cooloff_learning()  # sample anchors at 20 °C
    assert ctrl._cooloff_start[ZA] is not None
    advance(ctrl, 240)  # four hours later...
    _hall_temp(hass, 16)  # 4 °C lost over an average gap of 8 -> k = 0.125/h
    ctrl._update_cooloff_learning()
    # 20 %/h + 0.3 * (12.5 - 20) = 17.75 %/h
    assert ctrl.number("zone_a_heatloss_pct") == pytest.approx(17.75, abs=0.01)


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
    ctrl, hass = make_controller()
    hass.states.set(E["office"][0], "heat", {"current_temperature": 20, "temperature": 21})
    ctrl.applied[ZB] = PRESET_COMFORT
    ctrl._update_warmup_learning()
    assert ctrl._zone_target(ZB) == 21


def test_office_target_defaults_to_hall_slider_until_seen():
    ctrl, _ = make_controller()
    assert ctrl._zone_target(ZB) == ctrl.number("hall_comfort_temp")


def test_updated_cooling_k_thresholds():
    from custom_components.scout_hut_heating.preheat import updated_cooling_k

    assert updated_cooling_k(0.2, 4, 0.5, 8) == 0.2  # drop too small
    assert updated_cooling_k(0.2, 0.2, 2, 8) == 0.2  # duration too short
    assert updated_cooling_k(0.2, 2, 2, 2) == 0.2  # gap too small to normalise
    assert updated_cooling_k(0.2, 2, 2, 10) == pytest.approx(0.17)  # observed 0.1/h


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
    # the old constant-rate model.
    lead = required_lead_minutes(
        rate=20, indoor=19, target=22, outdoor=15, max_minutes=600,
        gap_hours=1000, cool_k=0.25,
    )
    assert lead == pytest.approx(20 * (22 - 15))


# --- Lateral spread: coldest-reading pre-heat + the spread diagnostic -----------

def _both_hall_temps(hass, a, b):
    hass.states.set(E["hall"][0], "heat", {"current_temperature": a})
    hass.states.set(E["hall"][1], "heat", {"current_temperature": b})


def test_preheat_sizes_for_the_coldest_end_of_the_hall():
    ctrl, hass = make_controller()
    _set_rate(ctrl, "hall_comfort_temp", 22)  # pin: tests the maths, not the default
    _set_rate(ctrl, "zone_a_warmup_rate", 20)
    _set_rate(ctrl, "zone_a_heatloss_pct", 0)
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


# --- Audit-fix regressions -------------------------------------------------------

def test_warmup_rate_ignores_implausibly_fast_samples():
    # A cloud-lagged reading catching up in one jump is not a real warm-up.
    assert updated_rate(20, 5, 4) == 20


def test_clamped_cooling_prediction_uses_the_full_floor_deficit():
    # Once the 7 °C floor binds, the room bottomed out long before the
    # pre-heat begins: the lead is rate x (target - floor), grown only by the
    # cold-weather margin (outdoor -5 -> +20%).
    lead = required_lead_minutes(
        rate=8, indoor=19, target=22, outdoor=-5, max_minutes=600,
        gap_hours=48, cool_k=0.25,
    )
    assert lead == pytest.approx(8 * 15 * 1.20)


def test_fast_heat_loss_is_learnable():
    from scout_testkit import PRESET_ICE

    ctrl, hass = make_controller()
    _set_rate(ctrl, "zone_a_heatloss_pct", 20)
    hass.states.set(E["weather"], "cloudy", {"temperature": 9})
    _hall_temp(hass, 20)
    ctrl.applied[ZA] = PRESET_ICE
    ctrl._update_cooloff_learning()
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
    assert ctrl._zone_preheat_minutes(ZA) == 60
    _set_rate(ctrl, "zone_a_warmup_rate_fans", 15)  # now trained
    assert ctrl._zone_preheat_minutes(ZA) == 45


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
