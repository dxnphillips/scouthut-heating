"""Seasonal lockout decision (3-day average temperature)."""

from custom_components.scout_hut_heating.coordinator import ScoutController

D = ScoutController._lockout_decision


def _days(high, low, n=3):
    return [{"temperature": high, "templow": low} for _ in range(n)]


def test_hot_days_cool_nights_locks_out():
    avg, warm, cold = D(_days(33, 15), 15, 20)
    assert warm is True and cold is False


def test_heatwave_with_cooler_nights_still_locks_out():
    avg, warm, _ = D(_days(31, 13), 15, 20)  # mean 22
    assert warm is True


def test_mild_day_cold_night_does_not_lock_out():
    avg, warm, cold = D(_days(16, 4), 15, 12)  # mean 10
    assert warm is False and cold is True


def test_winter_does_not_lock_out():
    _, warm, _ = D(_days(8, 2), 15, 5)
    assert warm is False


def test_borderline_average_equals_threshold_locks_out():
    _, warm, _ = D(_days(15, 15), 15, 20)  # mean 15 == threshold
    assert warm is True


def test_hysteresis_dead_band_neither_engages_nor_releases():
    # Just below the threshold: not warm enough to engage, but inside the
    # release band, so an engaged lockout stays engaged (no hourly flapping).
    _, warm, cold = D(_days(14.8, 14.8), 15, 20)  # mean 14.8, band 0.5
    assert warm is False and cold is False


def test_release_below_the_band():
    _, warm, cold = D(_days(14.5, 14.5), 15, 20)  # mean 14.5 == threshold - band
    assert warm is False and cold is True


def test_cold_realfeel_forces_release():
    _, _, cold = D(_days(33, 15), 15, 10)  # warm average but RealFeel below
    assert cold is True


def test_missing_low_uses_high():
    _, warm, _ = D([{"temperature": 20}, {"temperature": 20}, {"temperature": 20}], 15, 20)
    assert warm is True


def test_empty_forecast_returns_none():
    avg, warm, cold = D([], 15, 20)
    assert avg is None and warm is False and cold is False


def test_only_two_days_still_averaged():
    avg, warm, _ = D(_days(30, 16, n=2), 15, 20)
    assert avg == 23 and warm is True


def test_cold_realfeel_blocks_engage_so_lockout_cannot_flap():
    # A warm 3-day average with a cold-snap RealFeel used to satisfy engage
    # and release simultaneously, toggling the lockout every hourly check.
    _, warm, cold = D(_days(33, 15), 15, 10)
    assert warm is False and cold is True
