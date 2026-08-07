"""Booking "hold" — anticipatory maintain so a booked hall doesn't dip below
comfort as the evening cools.

The drive is slow and reactive (fires only below target), so on a falling evening
it catches the room at comfort and then lags, undershooting. During a booking we
know comfort is wanted for the whole slot, so the drive holds the hall a little
ABOVE comfort — a margin sized from the learned cool-off and warm-up rates, so it
is exactly the head-start the fall warrants (bigger on a cold night, ~zero on a
mild one), and self-zeroing/off when unlearned or capped to 0.
"""

from custom_components.scout_hut_heating.preheat import hold_margin
from scout_testkit import PRESET_COMFORT, PRESET_ICE, ZA, ZB, booking, hall_temp, make_controller, motion, preheat_window


# --- Pure maths -------------------------------------------------------------
def _m(**kw):
    base = dict(comfort=19.5, outdoor=5.0, cool_k=0.10, warmup_rate=45.0, cap=1.5)
    base.update(kw)
    return hold_margin(**base)


def test_cold_night_earns_a_positive_margin():
    # gap 14.5, cool 1.45/h, lead 20*45/30=30 min -> 0.725 C
    assert round(_m(), 3) == 0.725


def test_mild_night_earns_almost_nothing():
    assert _m(outdoor=19.0) < 0.05  # gap 0.5 -> tiny


def test_outdoor_at_or_above_comfort_is_zero():
    assert _m(outdoor=19.5) == 0.0
    assert _m(outdoor=25.0) == 0.0


def test_sluggish_hall_gets_a_bigger_margin_than_a_brisk_one():
    assert _m(warmup_rate=55.0) > _m(warmup_rate=20.0)


def test_faster_loss_gets_a_bigger_margin():
    assert _m(cool_k=0.15) > _m(cool_k=0.06)


def test_clamped_to_cap():
    assert _m(cool_k=0.3, outdoor=-10.0, warmup_rate=60.0) == 1.5  # would be huge


def test_disabled_by_zero_cap_or_unlearned_rates():
    assert _m(cap=0.0) == 0.0
    assert _m(cool_k=0.0) == 0.0
    assert _m(warmup_rate=0.0) == 0.0


def test_unreadable_outdoor_errs_warm():
    assert _m(outdoor=None) > 0.0  # assumes the cold fallback, so still holds


# --- Coordinator wiring -----------------------------------------------------
def _cold_evening_booking():
    ctrl, hass = make_controller()
    hass.states.set("weather.forecast", "cloudy", {"temperature": 5.0})  # cold night
    ctrl._numbers["zone_a_heatloss_pct"].native_value = 10.0
    ctrl._numbers["zone_a_warmup_rate"].native_value = 45.0
    ctrl._numbers["zone_a_warmup_rate_fans"].native_value = 45.0
    booking(ctrl, ZA)
    return ctrl, hass


def test_running_cold_booking_has_a_hold_margin():
    ctrl, _ = _cold_evening_booking()
    assert ctrl._booking_hold_margin(ZA) > 0.0
    # and the drive aims that far above comfort
    assert ctrl._drive_comfort_target(ZA) > ctrl.number("hall_comfort_temp")


def test_hold_stops_a_slightly_warm_booking_dropping():
    # Hall at 20.0 — above bare comfort (19.5) but the hold wants it higher, so it
    # keeps heating instead of icing, pre-empting the dip.
    ctrl, _ = _cold_evening_booking()
    motion(ctrl, "hall")
    hall_temp(ctrl, 20.0)
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT
    # With the hold disabled, the same room would ice (nothing to gain at 20.0).
    ctrl._numbers["booking_hold_cap"].native_value = 0
    assert ctrl._desired_zone(ZA) == PRESET_ICE


def test_pre_heat_window_has_no_hold_yet():
    # The hold is for holding the floor DURING the slot; the pre-heat owns arrival.
    ctrl, hass = make_controller()
    hass.states.set("weather.forecast", "cloudy", {"temperature": 5.0})
    preheat_window(ctrl, ZA)  # upcoming, not running
    assert ctrl._booking_hold_margin(ZA) == 0.0


def test_eco_keyword_booking_has_no_hold():
    ctrl, hass = _cold_evening_booking()
    ctrl.cal_title[ZA] = "test"  # ECO keyword -> aims at eco-low, not comfort
    assert ctrl._booking_hold_margin(ZA) == 0.0


def test_office_has_no_hold():
    ctrl, hass = make_controller()
    hass.states.set("weather.forecast", "cloudy", {"temperature": 5.0})
    booking(ctrl, ZB)
    assert ctrl._booking_hold_margin(ZB) == 0.0


def test_no_booking_no_hold():
    ctrl, hass = make_controller()
    hass.states.set("weather.forecast", "cloudy", {"temperature": 5.0})
    motion(ctrl, "hall")
    hall_temp(ctrl, 18.0)
    assert ctrl._booking_hold_margin(ZA) == 0.0  # occupancy stays reactive
