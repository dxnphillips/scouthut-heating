"""The unified, season-independent heat gate (`_room_wants_heat`).

Heating is no longer gated by the calendar season. A booked OR occupied room
heats toward the target it asks for whenever its own coldest probe is genuinely
below it, and lands on ice (freeing the cooling fans) when it is warm enough.
The season flag survives only for the condensation watch — it does not ice a
cold room. This is the collapse of the old seasonal-lockout + cold-booking
pierce + summer-setback trio into one rule.
"""

from scout_testkit import (
    E,
    PRESET_COMFORT,
    PRESET_ECO,
    PRESET_ICE,
    ZA,
    ZB,
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
    ctrl._numbers["booking_hold_cap"].native_value = 0  # isolate the base gate
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
    ctrl, hass = make_controller()
    hass.states.set("weather.forecast", "sunny", {"temperature": 22.0})  # warm -> no hold
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
    # A booking warms a cold shared zone to comfort (people use the kitchen/
    # toilets during a session), whatever the warm-season flag says.
    ctrl = _cold_hall_booking()
    assert ctrl._desired_shared() == PRESET_COMFORT
    assert ctrl._preset_reason["shared"] == "booking"


# --- The hall is judged on its AVERAGE, not its cold end (v1.47.0) -----------
def _hall_ends(ctrl, warm, cold):
    ctrl.hass.states.set(E["hall"][0], "heat", {"current_temperature": warm})
    ctrl.hass.states.set(E["hall"][1], "heat", {"current_temperature": cold})


def _booked_hall_at_19(ctrl):
    ctrl.hass.states.set(E["weather"], "sunny", {"temperature": 22.0})
    ctrl._numbers["hall_comfort_temp"].native_value = 19.0
    ctrl._numbers["booking_hold_cap"].native_value = 0  # isolate the base gate
    booking(ctrl, ZA)
    motion(ctrl, "hall")


def test_the_gate_releases_on_the_hall_average_not_the_cold_end():
    # 2026-09-28 Beavers: the average was 19.5 with the cold end at 18.5, and
    # holding comfort until the cold end reached 19.5 left the warm end at 20+
    # before the children added their own 0.75. Judged on the average, the room
    # releases at average >= target + band whatever the cold end reads.
    ctrl, _ = make_controller()
    _booked_hall_at_19(ctrl)
    ctrl.applied[ZA] = PRESET_COMFORT
    _hall_ends(ctrl, 20.0, 18.5)  # average 19.25: inside the band, still heats
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT
    _hall_ends(ctrl, 20.5, 18.5)  # average 19.5 = target + band: released
    assert ctrl._desired_zone(ZA) == PRESET_ICE
    assert ctrl._preset_reason[ZA] == "booking_warm"


def test_the_gate_engages_on_the_hall_average_not_the_cold_end():
    ctrl, _ = make_controller()
    _booked_hall_at_19(ctrl)
    ctrl.applied[ZA] = PRESET_ICE
    _hall_ends(ctrl, 20.0, 18.0)  # cold end short, average 19.0 at target: no heat
    assert ctrl._desired_zone(ZA) == PRESET_ICE
    _hall_ends(ctrl, 19.5, 18.0)  # average 18.75: the room as a whole is short
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT


def test_arrival_shortfall_is_judged_on_the_average_with_the_cold_end_beside_it():
    ctrl, _ = make_controller()
    ctrl._numbers["hall_comfort_temp"].native_value = 19.0
    _hall_ends(ctrl, 20.0, 18.0)
    ctrl._record_booking_edges()  # baseline observed: calendar off
    booking(ctrl, ZA)
    ctrl._record_booking_edges()
    (evt,) = [e for e in ctrl.audit.to_list() if e.get("event") == "booking_start"]
    assert evt["average"] == 19.0 and evt["coldest"] == 18.0
    assert evt["shortfall"] == 0.0
