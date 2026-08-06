"""F6: fan cooling-vs-destratify direction follows room state, not the season.

`_fan_cooling_regime(warm, heating)` returns whether the COOLING (forward)
regime is wanted. Default is season-derived (`_summer_active`); with
`fans_follow_state` on it follows the hall's thermal state, decoupled from the
3-day-average seasonal lockout.
"""

from scout_testkit import make_controller


def _ctrl(follow_state=False, summer_mode=False, lockout=False):
    ctrl, _ = make_controller()
    ctrl._switches["fans_follow_state"].is_on = follow_state
    ctrl._switches["summer_mode"].is_on = summer_mode
    ctrl.seasonal_lockout = lockout
    return ctrl


# --- Default (season-derived) ---------------------------------------------
def test_default_follows_season_lockout_on():
    ctrl = _ctrl(follow_state=False, lockout=True)
    assert ctrl._fan_cooling_regime(warm=False, heating=False) is True


def test_default_follows_season_lockout_off():
    ctrl = _ctrl(follow_state=False, lockout=False)
    assert ctrl._fan_cooling_regime(warm=True, heating=False) is False


# --- State-based ----------------------------------------------------------
def test_state_warm_and_not_heating_cools():
    ctrl = _ctrl(follow_state=True)
    assert ctrl._fan_cooling_regime(warm=True, heating=False) is True


def test_state_warm_but_heating_destratifies():
    ctrl = _ctrl(follow_state=True)
    assert ctrl._fan_cooling_regime(warm=True, heating=True) is False


def test_state_cool_destratifies():
    ctrl = _ctrl(follow_state=True)
    assert ctrl._fan_cooling_regime(warm=False, heating=False) is False


def test_state_unknown_warmth_never_cools():
    ctrl = _ctrl(follow_state=True)
    assert ctrl._fan_cooling_regime(warm=None, heating=False) is False


def test_state_manual_summer_mode_forces_cooling():
    ctrl = _ctrl(follow_state=True, summer_mode=True)
    # Even cool + not heating: the manual force wins.
    assert ctrl._fan_cooling_regime(warm=False, heating=False) is True


# --- The decoupling (the whole point of F6) --------------------------------
def test_state_cools_in_winter_season_when_warm():
    """Lockout released (=winter) but a warm hall still gets cooling."""
    ctrl = _ctrl(follow_state=True, lockout=False)
    assert ctrl._fan_cooling_regime(warm=True, heating=False) is True


def test_state_destratifies_in_summer_season_when_cool():
    """Lockout engaged (=summer) but a cool hall still destratifies, not cools."""
    ctrl = _ctrl(follow_state=True, lockout=True)
    assert ctrl._fan_cooling_regime(warm=False, heating=False) is False
