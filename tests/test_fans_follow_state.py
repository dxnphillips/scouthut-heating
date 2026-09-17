"""Fan cooling-vs-destratify direction is fully automatic from room state.

`_fan_cooling_regime(warm, heating)` returns whether the COOLING (forward) regime
is wanted — with no toggle and no season: heating always destratifies, a
genuinely warm unheated hall cools, a cool one destratifies. Both the enter and
release lines hang off ONE comfort reference (`_cooling_reference`), so they can
never be set into conflict with the heating line; the release band (keyed off the
previous fan_mode) stops the heavy fans flapping forward<->reverse.
"""

from custom_components.scout_hut_heating.const import COOLING_RELEASE_FLOOR
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


def _lines(ctrl):
    """The enter / release temperatures the current settings imply."""
    ref = ctrl._cooling_reference()
    offset = ctrl.number("cooling_above_comfort")
    return ref + offset, ref + max(offset / 2, COOLING_RELEASE_FLOOR)


def test_warm_needs_to_exceed_the_threshold_to_start_cooling():
    ctrl, _ = _mk()
    ctrl.fan_mode = "off"  # not currently cooling
    enter, _release = _lines(ctrl)
    # Uniform room a touch below the enter line -> not warm.
    _hall(ctrl, enter - 0.4, enter - 0.4)
    assert _warm_flag(ctrl) is False
    # Above it -> warm.
    _hall(ctrl, enter + 1.0, enter + 1.0)
    assert _warm_flag(ctrl) is True


def test_hysteresis_holds_cooling_below_the_threshold():
    ctrl, _ = _mk()
    enter, release = _lines(ctrl)
    # Already cooling: stays warm until the room falls past the release line.
    ctrl.fan_mode = "summer"
    _hall(ctrl, enter - 0.25, enter - 0.25)  # below enter, above release
    assert _warm_flag(ctrl) is True  # still cooling (no flap)
    _hall(ctrl, release - 0.25, release - 0.25)
    assert _warm_flag(ctrl) is False


# --- One reference: both lines follow comfort, and never meet it -------------
def test_the_cooling_lines_follow_the_comfort_slider():
    ctrl, _ = _mk()
    ctrl._numbers["hall_comfort_temp"].native_value = 19.0
    ctrl._numbers["cooling_above_comfort"].native_value = 1.0
    assert _lines(ctrl) == (20.0, 19.5)
    # Raise comfort and the whole cooling band moves with it — one number to set.
    ctrl._numbers["hall_comfort_temp"].native_value = 21.0
    assert _lines(ctrl) == (22.0, 21.5)


def test_the_release_line_can_never_reach_the_heating_line():
    """The 2026-09-17 chase: release == comfort, so the breeze cooled a booked
    hall onto its own heating trigger. Unreachable now at any offset."""
    ctrl, _ = _mk()
    comfort = 19.0
    ctrl._numbers["hall_comfort_temp"].native_value = comfort
    for offset in (1.0, 1.5, 2.0, 4.0, 8.0):
        ctrl._numbers["cooling_above_comfort"].native_value = offset
        enter, release = _lines(ctrl)
        assert release >= comfort + COOLING_RELEASE_FLOOR
        assert enter > release


def test_an_eco_booking_gets_the_breeze_at_the_ordinary_comfort_line():
    """An ECO keyword lowers what we will SPEND, not what people find pleasant:
    a hirer sitting in a 21 C hall is as warm as any other group at 21."""
    from scout_testkit import ZA, booking

    ctrl, _ = _mk()
    ctrl._numbers["hall_comfort_temp"].native_value = 19.0
    booking(ctrl, ZA, "sal-vation cleaning")
    assert ctrl._eco_keyword_active(ZA) is True
    assert ctrl._booking_target(ZA) == ctrl.number("hall_eco_low_temp")  # 14: the heat goal
    assert ctrl._cooling_reference() == 19.0  # but comfort is what the breeze reads
    assert _lines(ctrl) == (20.0, 19.5)


def test_a_boost_lifts_the_cooling_reference_with_it():
    """Someone asked for warmer: the breeze must not fight the heat they asked
    for. (A boost also forces the heating preset, which suppresses the cooling
    regime anyway — this is the belt-and-braces half.)"""
    from datetime import timedelta

    from scout_testkit import ZA

    ctrl, _ = _mk()
    ctrl._numbers["hall_comfort_temp"].native_value = 19.0
    assert ctrl._cooling_reference() == 19.0
    ctrl.boost_until[ZA] = ctrl._now() + timedelta(minutes=30)
    assert ctrl._cooling_reference() == 19.0 + ctrl.number("boost_offset")
