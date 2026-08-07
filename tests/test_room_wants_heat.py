"""The unified, season-independent heat gate (`_room_wants_heat`).

Heating is no longer gated by the calendar season. A booked OR occupied room
heats toward the target it asks for whenever its own coldest probe is genuinely
below it, and lands on ice (freeing the cooling fans) when it is warm enough.
The season flag survives only for the condensation watch — it does not ice a
cold room. This is the collapse of the old seasonal-lockout + cold-booking
pierce + summer-setback trio into one rule.
"""

from scout_testkit import (
    PRESET_COMFORT,
    PRESET_ECO,
    PRESET_ICE,
    ZA,
    ZB,
    E,
    booking,
    hall_temp,
    make_controller,
    motion,
    preheat_window,
    zone_temp,
)


def _cold_hall_booking(started=True, lockout=True):
    """A cold hall booking, with the warm-season flag optionally set (it must
    make no difference to the heating decision)."""
    ctrl, _ = make_controller()
    ctrl.seasonal_lockout = lockout
    if started:
        booking(ctrl, ZA)
    else:
        preheat_window(ctrl, ZA)
    hall_temp(ctrl, 12.0)  # far below the 19.5 comfort target
    return ctrl


# --- A cold booking heats, whatever the season ------------------------------
def test_cold_booking_heats_under_the_warm_season_flag():
    ctrl = _cold_hall_booking()
    motion(ctrl, "hall")
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT


def test_cold_booking_reason_is_plain_booking_no_lockout_tag():
    ctrl = _cold_hall_booking()
    motion(ctrl, "hall")
    ctrl._desired_zone(ZA)
    assert ctrl._preset_reason[ZA] == "booking"


def test_warm_booking_ices_for_the_cooling_fans():
    """A booking already at/above target needs no heat — ice frees the fans."""
    ctrl, _ = make_controller()
    booking(ctrl, ZA)
    motion(ctrl, "hall")
    hall_temp(ctrl, 20.5)  # above the 19.5 target — warm fabric, no heat needed
    assert ctrl._desired_zone(ZA) == PRESET_ICE
    assert ctrl._preset_reason[ZA] == "booking_warm"


def test_no_occupancy_no_booking_stays_ice():
    ctrl, _ = make_controller()
    ctrl.seasonal_lockout = True
    hall_temp(ctrl, 12.0)  # cold, but nobody is here or booked
    assert ctrl._desired_zone(ZA) == PRESET_ICE
    assert ctrl._preset_reason[ZA] == "building_empty"


def test_unreadable_booked_room_errs_warm():
    """No reading + a booking -> heat (err warm). The Rointe governs the real
    firing against its own probe, so a genuinely warm room will not fire; a cold
    one that merely lost our floor sensor is not left to arrive cold."""
    ctrl, _ = make_controller()
    booking(ctrl, ZA)
    motion(ctrl, "hall")
    # Hall heaters never report a temperature.
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT


# --- Pre-heat window included -----------------------------------------------
def test_cold_preheat_window_heats():
    ctrl = _cold_hall_booking(started=False)
    # No running event and nobody there yet: still comfort during pre-heat.
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT
    assert ctrl._preset_reason[ZA] == "preheat"


# --- ECO-keyword bookings judged against the eco-low target -----------------
def test_cold_eco_booking_uses_eco_low_target():
    ctrl, _ = make_controller()
    ctrl.seasonal_lockout = True
    booking(ctrl, ZA, "Test event")  # 'test' is a default ECO keyword
    hall_temp(ctrl, 12.0)  # below eco-low (14)
    assert ctrl._desired_zone(ZA) == PRESET_ECO
    assert ctrl._preset_reason[ZA] == "booking_eco"


def test_eco_booking_at_eco_low_target_ices():
    ctrl, _ = make_controller()
    booking(ctrl, ZA, "Test event")
    hall_temp(ctrl, 15.0)  # above eco-low (14) — no heat needed
    assert ctrl._desired_zone(ZA) == PRESET_ICE
    assert ctrl._preset_reason[ZA] == "booking_warm"


# --- Release hysteresis -----------------------------------------------------
def test_heat_holds_until_release_band_once_heating():
    ctrl = _cold_hall_booking()
    motion(ctrl, "hall")
    ctrl.applied[ZA] = PRESET_COMFORT  # already heating
    hall_temp(ctrl, 19.8)  # above target but within the 0.5 release band
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT


def test_heat_releases_above_the_band():
    ctrl = _cold_hall_booking()
    motion(ctrl, "hall")
    ctrl.applied[ZA] = PRESET_COMFORT
    hall_temp(ctrl, 20.1)  # past target + release band
    assert ctrl._desired_zone(ZA) == PRESET_ICE


# --- Office -----------------------------------------------------------------
def test_cold_office_booking_heats():
    ctrl, _ = make_controller()
    ctrl.seasonal_lockout = True
    booking(ctrl, ZB)
    motion(ctrl, "office")
    zone_temp(ctrl, ZB, 12.0)
    assert ctrl._desired_zone(ZB) == PRESET_COMFORT


# --- Shared zone follows ----------------------------------------------------
def test_shared_follows_a_hall_booking_regardless_of_season():
    ctrl = _cold_hall_booking()
    assert ctrl._desired_shared() == PRESET_ECO
    assert ctrl._preset_reason["shared"] == "booking"
