"""Cold bookings pierce the seasonal lockout (the `cold_booking_heats` switch).

The summer lockout freezes heating for the season, but a booked (or pre-heating)
session whose room is genuinely below the target it is asking for should still be
warmed. "Cold" is read from the room's own coldest heater probe against the
booking target (self-calibrating — no weather constant), so a warm-fabric summer
booking already at target stays locked out. Covers the pre-heat window, follows
into the shared zone, and sits behind a default-on switch.
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
    preheat_window,
)


def _hall_temp(ctrl, temp):
    """Report the same current temperature on every hall heater."""
    for eid in E["hall"]:
        ctrl.hass.states.set(eid, "heat", {"current_temperature": temp})


def _office_temp(ctrl, temp):
    for eid in E["office"]:
        ctrl.hass.states.set(eid, "heat", {"current_temperature": temp})


def _cold_booking(started=True):
    """Controller under summer lockout with a cold hall booking."""
    ctrl, _ = make_controller()
    ctrl.seasonal_lockout = True
    if started:
        booking(ctrl, ZA)
    else:
        preheat_window(ctrl, ZA)
    _hall_temp(ctrl, 12.0)  # far below the 19.5 comfort target
    return ctrl


# --- The core bypass -------------------------------------------------------
def test_cold_booking_pierces_lockout():
    ctrl = _cold_booking()
    motion(ctrl, "hall")
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT


def test_cold_booking_reason_is_tagged():
    ctrl = _cold_booking()
    motion(ctrl, "hall")
    ctrl._desired_zone(ZA)
    assert ctrl._preset_reason[ZA] == "lockout_booking"


def test_warm_booking_stays_locked_out():
    """A summer booking whose room is already at/above target does not bypass."""
    ctrl, _ = make_controller()
    ctrl.seasonal_lockout = True
    booking(ctrl, ZA)
    motion(ctrl, "hall")
    _hall_temp(ctrl, 20.5)  # above the 19.5 target — warm fabric, no heat needed
    assert ctrl._desired_zone(ZA) == PRESET_ICE
    assert ctrl._preset_reason[ZA] == "seasonal_lockout"


def test_no_booking_under_lockout_stays_ice():
    ctrl, _ = make_controller()
    ctrl.seasonal_lockout = True
    _hall_temp(ctrl, 12.0)  # cold, but nobody has booked
    assert ctrl._desired_zone(ZA) == PRESET_ICE


def test_unreadable_room_does_not_bypass():
    """No temperature reading -> stay locked (summer fail-safe; Boost still works)."""
    ctrl, _ = make_controller()
    ctrl.seasonal_lockout = True
    booking(ctrl, ZA)
    motion(ctrl, "hall")
    # Hall heaters never report a temperature.
    assert ctrl._desired_zone(ZA) == PRESET_ICE


# --- Pre-heat window included ---------------------------------------------
def test_cold_preheat_window_pierces_lockout():
    ctrl = _cold_booking(started=False)
    # No running event and nobody there yet: still comfort during pre-heat.
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT
    assert ctrl._preset_reason[ZA] == "lockout_preheat"


# --- ECO-keyword bookings judged against the eco-low target ---------------
def test_cold_eco_booking_uses_eco_low_target():
    ctrl, _ = make_controller()
    ctrl.seasonal_lockout = True
    booking(ctrl, ZA, "Test event")  # 'test' is a default ECO keyword
    _hall_temp(ctrl, 12.0)  # below eco-low (14)
    assert ctrl._desired_zone(ZA) == PRESET_ECO
    assert ctrl._preset_reason[ZA] == "lockout_booking_eco"


def test_eco_booking_at_eco_low_target_stays_locked():
    ctrl, _ = make_controller()
    ctrl.seasonal_lockout = True
    booking(ctrl, ZA, "Test event")
    _hall_temp(ctrl, 15.0)  # above eco-low (14) — no heat needed
    assert ctrl._desired_zone(ZA) == PRESET_ICE


# --- Release hysteresis ----------------------------------------------------
def test_bypass_holds_until_release_band_once_heating():
    ctrl = _cold_booking()
    motion(ctrl, "hall")
    ctrl.applied[ZA] = PRESET_COMFORT  # already heating under the bypass
    _hall_temp(ctrl, 19.8)  # above target but within the 0.5 release band
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT


def test_bypass_releases_above_the_band():
    ctrl = _cold_booking()
    motion(ctrl, "hall")
    ctrl.applied[ZA] = PRESET_COMFORT
    _hall_temp(ctrl, 20.1)  # past target + release band
    assert ctrl._desired_zone(ZA) == PRESET_ICE


# --- The off switch --------------------------------------------------------
def test_switch_off_restores_strict_lockout():
    ctrl = _cold_booking()
    motion(ctrl, "hall")
    ctrl._switches["cold_booking_heats"].is_on = False
    assert ctrl._desired_zone(ZA) == PRESET_ICE
    assert ctrl._preset_reason[ZA] == "seasonal_lockout"


# --- Office ----------------------------------------------------------------
def test_cold_office_booking_pierces_lockout():
    ctrl, _ = make_controller()
    ctrl.seasonal_lockout = True
    booking(ctrl, ZB)
    motion(ctrl, "office")
    _office_temp(ctrl, 12.0)
    assert ctrl._desired_zone(ZB) == PRESET_COMFORT


# --- Shared zone follows ---------------------------------------------------
def test_shared_follows_a_cold_hall_booking():
    ctrl = _cold_booking()
    assert ctrl._desired_shared() == PRESET_ECO
    assert ctrl._preset_reason["shared"] == "lockout_booking"


def test_shared_stays_locked_when_no_zone_bypasses():
    ctrl, _ = make_controller()
    ctrl.seasonal_lockout = True
    booking(ctrl, ZA)
    _hall_temp(ctrl, 20.5)  # warm hall booking — no bypass
    assert ctrl._desired_shared() == PRESET_ICE
