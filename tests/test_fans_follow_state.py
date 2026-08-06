"""Cooling changeover select: how the fan cooling-vs-destratify direction is chosen.

`_fan_cooling_regime(warm, heating)` returns whether the COOLING (forward) regime
is wanted, from the single `cooling_changeover` select (Never cool / Follow
season / Follow room state / Always cool). This replaced the old summer_mode /
summer_follows_season / fans_follow_state switch tangle.
"""

from scout_testkit import (
    COOLING_ALWAYS,
    COOLING_FOLLOW_SEASON,
    COOLING_FOLLOW_STATE,
    COOLING_NEVER,
    make_controller,
    set_cooling,
)


def _ctrl(mode=COOLING_FOLLOW_SEASON, lockout=False):
    ctrl, _ = make_controller()
    set_cooling(ctrl, mode)
    ctrl.seasonal_lockout = lockout
    return ctrl


# --- Follow season (the default) ------------------------------------------
def test_follow_season_cools_when_lockout_engaged():
    ctrl = _ctrl(COOLING_FOLLOW_SEASON, lockout=True)
    assert ctrl._fan_cooling_regime(warm=False, heating=False) is True


def test_follow_season_destratifies_when_lockout_off():
    ctrl = _ctrl(COOLING_FOLLOW_SEASON, lockout=False)
    assert ctrl._fan_cooling_regime(warm=True, heating=False) is False


# --- Never / Always -------------------------------------------------------
def test_never_cool_always_destratifies():
    ctrl = _ctrl(COOLING_NEVER, lockout=True)  # even in summer season
    assert ctrl._fan_cooling_regime(warm=True, heating=False) is False


def test_always_cool_forces_cooling():
    ctrl = _ctrl(COOLING_ALWAYS, lockout=False)  # even cool + winter season
    assert ctrl._fan_cooling_regime(warm=False, heating=False) is True


# --- Follow room state ----------------------------------------------------
def test_state_warm_and_not_heating_cools():
    ctrl = _ctrl(COOLING_FOLLOW_STATE)
    assert ctrl._fan_cooling_regime(warm=True, heating=False) is True


def test_state_warm_but_heating_destratifies():
    ctrl = _ctrl(COOLING_FOLLOW_STATE)
    assert ctrl._fan_cooling_regime(warm=True, heating=True) is False


def test_state_cool_destratifies():
    ctrl = _ctrl(COOLING_FOLLOW_STATE)
    assert ctrl._fan_cooling_regime(warm=False, heating=False) is False


def test_state_unknown_warmth_never_cools():
    ctrl = _ctrl(COOLING_FOLLOW_STATE)
    assert ctrl._fan_cooling_regime(warm=None, heating=False) is False


# --- The decoupling from season -------------------------------------------
def test_state_cools_in_winter_season_when_warm():
    ctrl = _ctrl(COOLING_FOLLOW_STATE, lockout=False)  # winter season
    assert ctrl._fan_cooling_regime(warm=True, heating=False) is True


def test_state_destratifies_in_summer_season_when_cool():
    ctrl = _ctrl(COOLING_FOLLOW_STATE, lockout=True)  # summer season
    assert ctrl._fan_cooling_regime(warm=False, heating=False) is False


# --- _summer_active (season-scoped uses) follows the select ---------------
def test_summer_active_follows_season_option():
    ctrl = _ctrl(COOLING_FOLLOW_SEASON, lockout=True)
    assert ctrl._summer_active() is True
    ctrl.seasonal_lockout = False
    assert ctrl._summer_active() is False


def test_summer_active_never_and_always():
    ctrl = _ctrl(COOLING_NEVER, lockout=True)
    assert ctrl._summer_active() is False
    set_cooling(ctrl, COOLING_ALWAYS)
    ctrl.seasonal_lockout = False
    assert ctrl._summer_active() is True


def test_summer_active_state_mode_is_season_derived():
    """Follow-room-state affects fan DIRECTION only; the season-scoped
    _summer_active still tracks the lockout."""
    ctrl = _ctrl(COOLING_FOLLOW_STATE, lockout=True)
    assert ctrl._summer_active() is True
