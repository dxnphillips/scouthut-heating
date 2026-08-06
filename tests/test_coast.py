"""Unit tests for the passive-arrival ("will it get there alone?") predictor.

Comfort-lean by construction: it answers "coast" only on a measured idle-room
climb that reaches the band with a time margin; every uncertain input answers
"heat".
"""

from custom_components.scout_hut_heating.coast import (
    DEADBAND,
    MIN_RISE_RATE,
    TIME_MARGIN_FRAC,
    will_coast_to_target,
)


def _coast(indoor, target, rise_rate, gap_min):
    return will_coast_to_target(
        indoor=indoor, target=target, rise_rate=rise_rate, gap_min=gap_min
    )


# --- Missing inputs always heat -------------------------------------------
def test_no_reading_heats():
    assert _coast(None, 19.5, 0.05, 120) is False


def test_no_rate_heats():
    assert _coast(17.0, 19.5, None, 120) is False


def test_no_gap_heats():
    assert _coast(17.0, 19.5, 0.05, None) is False


# --- Must be measurably warming -------------------------------------------
def test_static_room_heats():
    """Zero rise -> not warming -> heat, even with hours to spare."""
    assert _coast(17.0, 19.5, 0.0, 600) is False


def test_cooling_room_heats():
    assert _coast(19.0, 19.5, -0.02, 600) is False


def test_below_min_rise_heats():
    assert _coast(17.0, 19.5, MIN_RISE_RATE - 0.001, 600) is False


# --- The predictive branch -------------------------------------------------
def test_warming_reaches_band_with_margin_coasts():
    # deficit to band = 19.5 - 0.3 - 17.0 = 2.2 °C; at 0.1 °C/min -> 22 min,
    # * 1.3 margin = 28.6 min <= 120 -> coast.
    assert _coast(17.0, 19.5, 0.1, 120) is True


def test_warming_too_slow_to_arrive_heats():
    # deficit 2.2 °C at 0.01 °C/min -> 220 min, well past a 60-min gap.
    assert _coast(17.0, 19.5, 0.01, 60) is False


def test_already_in_band_and_rising_coasts():
    assert _coast(19.4, 19.5, 0.05, 120) is True


def test_time_margin_is_enforced():
    """Arrives exactly at the deadline without the margin -> still heat."""
    # deficit to band = 2.2 °C, rate 0.1 -> needed 22 min; needed*1.3 = 28.6.
    # A gap between 22 and 28.6 arrives in time bare but fails the margin.
    assert _coast(17.0, 19.5, 0.1, 25) is False
    assert _coast(17.0, 19.5, 0.1, 30) is True


def test_deadband_lets_a_near_miss_coast():
    """The band is target - DEADBAND, so it need not reach the bare setpoint."""
    # Just reaching target - DEADBAND counts as arrived.
    band = 19.5 - DEADBAND
    # indoor already at band, rising -> coast.
    assert _coast(band, 19.5, MIN_RISE_RATE, 120) is True


def test_margin_constant_is_positive():
    assert TIME_MARGIN_FRAC > 0
