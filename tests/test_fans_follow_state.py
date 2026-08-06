"""Fan cooling-vs-destratify direction is fully automatic from room state.

`_fan_cooling_regime(warm, heating)` returns whether the COOLING (forward) regime
is wanted — with no toggle and no season: heating always destratifies, a
genuinely warm unheated hall cools, a cool one destratifies. The warm/cool
boundary has hysteresis (keyed off the previous fan_mode) so the heavy fans
don't flap forward<->reverse at the threshold.
"""

from custom_components.scout_hut_heating.const import COOLING_DIRECTION_HYST
from scout_testkit import make_controller


def _ctrl():
    ctrl, _ = make_controller()
    return ctrl


# --- Pure state, no season ------------------------------------------------
def test_warm_and_not_heating_cools():
    ctrl = _ctrl()
    assert ctrl._fan_cooling_regime(warm=True, heating=False) is True


def test_warm_but_heating_destratifies():
    ctrl = _ctrl()
    assert ctrl._fan_cooling_regime(warm=True, heating=True) is False


def test_cool_destratifies():
    ctrl = _ctrl()
    assert ctrl._fan_cooling_regime(warm=False, heating=False) is False


def test_unknown_warmth_never_cools():
    ctrl = _ctrl()
    assert ctrl._fan_cooling_regime(warm=None, heating=False) is False


def test_direction_ignores_the_season():
    """The seasonal lockout no longer steers the fans at all."""
    ctrl = _ctrl()
    ctrl.seasonal_lockout = True  # "summer"
    assert ctrl._fan_cooling_regime(warm=False, heating=False) is False  # cool -> destrat
    ctrl.seasonal_lockout = False  # "winter"
    assert ctrl._fan_cooling_regime(warm=True, heating=False) is True  # warm -> cool


# --- Warm-boundary hysteresis (via the _fan_target warm computation) -------
def _hall(ctrl, floor, ceiling):
    from scout_testkit import E

    for eid in E["hall"]:
        ctrl.hass.states.set(eid, "heat", {"current_temperature": floor})
    ctrl.hass.states.set("sensor.ceiling", str(ceiling))


def _warm_flag(ctrl):
    """Run _fan_target and return the computed warm flag."""
    ctrl._fan_target()
    return ctrl._fan_warm


def _mk():
    from custom_components.scout_hut_heating.const import CONF_CEILING_TEMP, CONF_FAN_MASTER

    ctrl, hass = make_controller(
        config_overrides={CONF_FAN_MASTER: "switch.fan", CONF_CEILING_TEMP: "sensor.ceiling"}
    )
    hass.states.set("switch.fan", "on")
    return ctrl, hass


def test_warm_needs_to_exceed_the_threshold_to_start_cooling():
    ctrl, _ = _mk()
    ctrl.fan_mode = "off"  # not currently cooling
    high = ctrl.number("cooling_temp_high")  # 23
    # Uniform room a touch below the threshold -> not warm.
    _hall(ctrl, high - 0.4, high - 0.4)
    assert _warm_flag(ctrl) is False
    # Above the threshold -> warm.
    _hall(ctrl, high + 1.0, high + 1.0)
    assert _warm_flag(ctrl) is True


def test_hysteresis_holds_cooling_below_the_threshold():
    ctrl, _ = _mk()
    high = ctrl.number("cooling_temp_high")
    # Already cooling: stays warm until a full band below the threshold.
    ctrl.fan_mode = "summer"
    _hall(ctrl, high - 0.5, high - 0.5)  # below high, but within the hysteresis band
    assert _warm_flag(ctrl) is True  # still cooling (no flap)
    # Drop past the band -> cooling releases.
    _hall(ctrl, high - COOLING_DIRECTION_HYST - 0.5, high - COOLING_DIRECTION_HYST - 0.5)
    assert _warm_flag(ctrl) is False
