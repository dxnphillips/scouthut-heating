"""State-based summer setback (the `summer_setback_mode` switch).

The seasonal lockout normally freezes the hall solid for the warm season. With
summer setback on, the hall instead follows the building's *state*: an occupied
hall that is genuinely cool is gently warmed to a low setback floor
(`hall_summer_comfort_temp`, ~17.5 °C, delivered via the eco preset because the
Rointe comfort setpoint floor is 19), while a warm or empty hall still lands on
ice so the summer cooling fans keep the room. Hall-only, off by default, and a
genuinely cold booking still wins full comfort through the cold-booking pierce.
"""

from scout_testkit import (
    PRESET_COMFORT,
    PRESET_ECO,
    PRESET_ICE,
    ZA,
    ZB,
    E,
    booking,
    make_controller,
    motion,
)


def _hall_temp(ctrl, temp):
    for eid in E["hall"]:
        ctrl.hass.states.set(eid, "heat", {"current_temperature": temp})


def _office_temp(ctrl, temp):
    for eid in E["office"]:
        ctrl.hass.states.set(eid, "heat", {"current_temperature": temp})


def _setback(ctrl, on=True):
    ctrl._switches["summer_setback_mode"].is_on = on


def _occupied_cool_hall():
    """Hall under summer lockout, setback on, occupied and cool."""
    ctrl, _ = make_controller()
    ctrl.seasonal_lockout = True
    _setback(ctrl)
    motion(ctrl, "hall")
    _hall_temp(ctrl, 15.0)  # below the 17.5 setback floor
    return ctrl


# --- Off by default --------------------------------------------------------
def test_off_by_default_leaves_strict_lockout():
    ctrl, _ = make_controller()
    ctrl.seasonal_lockout = True
    motion(ctrl, "hall")
    _hall_temp(ctrl, 15.0)
    assert not ctrl._switches["summer_setback_mode"].is_on
    assert ctrl._desired_zone(ZA) == PRESET_ICE
    assert ctrl._preset_reason[ZA] == "seasonal_lockout"


# --- The core rung ---------------------------------------------------------
def test_occupied_cool_hall_gets_setback_heat():
    ctrl = _occupied_cool_hall()
    assert ctrl._desired_zone(ZA) == PRESET_ECO
    assert ctrl._preset_reason[ZA] == "summer_setback"


def test_setback_eco_carries_the_setback_floor():
    """Eco is pushed at the setback number, not the ordinary (winter) eco."""
    ctrl = _occupied_cool_hall()
    ctrl._desired_zone(ZA)  # sets the preset reason to summer_setback
    assert ctrl._hall_eco_target(eco_low=False) == ctrl.number("hall_summer_comfort_temp")
    # And that really differs from the ordinary eco number.
    assert ctrl.number("hall_summer_comfort_temp") != ctrl.number("hall_eco_temp")


def test_warm_hall_stays_ice_for_the_cooling_fans():
    ctrl = _occupied_cool_hall()
    _hall_temp(ctrl, 18.0)  # above the 17.5 setback floor
    assert ctrl._desired_zone(ZA) == PRESET_ICE
    assert ctrl._preset_reason[ZA] == "seasonal_lockout"


def test_empty_hall_stays_ice():
    """Setback needs occupancy: an empty hall is not heated on ambient warmth."""
    ctrl, _ = make_controller()
    ctrl.seasonal_lockout = True
    _setback(ctrl)
    _hall_temp(ctrl, 15.0)  # cool, but nobody there
    assert ctrl._desired_zone(ZA) == PRESET_ICE


def test_occupied_override_counts_as_occupancy():
    ctrl, _ = make_controller()
    ctrl.seasonal_lockout = True
    _setback(ctrl)
    ctrl._switches["zone_a_occupied_override"].is_on = True
    _hall_temp(ctrl, 15.0)
    assert ctrl._desired_zone(ZA) == PRESET_ECO
    assert ctrl._preset_reason[ZA] == "summer_setback"


def test_unreadable_room_does_not_heat():
    """No reading -> stay locked (summer fail-safe: err off)."""
    ctrl, _ = make_controller()
    ctrl.seasonal_lockout = True
    _setback(ctrl)
    motion(ctrl, "hall")
    # Hall heaters never report a temperature.
    assert ctrl._desired_zone(ZA) == PRESET_ICE


# --- Release hysteresis ----------------------------------------------------
def test_setback_holds_within_release_band_once_heating():
    ctrl = _occupied_cool_hall()
    ctrl.applied[ZA] = PRESET_ECO  # already warming under the setback
    _hall_temp(ctrl, 17.8)  # above 17.5 but within the 0.5 release band
    assert ctrl._desired_zone(ZA) == PRESET_ECO


def test_setback_releases_above_the_band():
    ctrl = _occupied_cool_hall()
    ctrl.applied[ZA] = PRESET_ECO
    _hall_temp(ctrl, 18.1)  # past setback + release band
    assert ctrl._desired_zone(ZA) == PRESET_ICE


# --- Interactions ----------------------------------------------------------
def test_cold_booking_still_wins_full_comfort():
    """A genuinely cold booking pierces to comfort, not the setback floor."""
    ctrl, _ = make_controller()
    ctrl.seasonal_lockout = True
    _setback(ctrl)
    booking(ctrl, ZA)
    motion(ctrl, "hall")
    _hall_temp(ctrl, 12.0)  # below the 19.5 booking target
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT
    assert ctrl._preset_reason[ZA] == "lockout_booking"


def test_office_has_no_setback():
    """Summer setback is hall-only: an occupied cool office still ices."""
    ctrl, _ = make_controller()
    ctrl.seasonal_lockout = True
    _setback(ctrl)
    motion(ctrl, "office")
    _office_temp(ctrl, 15.0)
    assert ctrl._desired_zone(ZB) == PRESET_ICE
    assert ctrl._preset_reason[ZB] == "seasonal_lockout"


def test_eco_target_is_ordinary_eco_outside_setback():
    """The eco router only lifts to the setback floor when that is the reason."""
    ctrl, _ = make_controller()
    # Motion, no lockout: ordinary occupied eco.
    motion(ctrl, "hall")
    ctrl._desired_zone(ZA)
    assert ctrl._preset_reason[ZA] != "summer_setback"
    assert ctrl._hall_eco_target(eco_low=False) == ctrl.number("hall_eco_temp")
