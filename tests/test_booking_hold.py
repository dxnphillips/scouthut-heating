"""Booking "hold" — anticipatory maintain so a booked hall doesn't dip below
comfort as the evening cools.

The drive is slow and reactive (fires only below target), so on a falling evening
it catches the room at comfort and then lags, undershooting. During a booking we
know comfort is wanted for the whole slot, so the heating gate engages and
releases a little ABOVE comfort — a margin sized from the learned cool-off and
warm-up rates, so it is exactly the head-start the fall warrants (bigger on a
cold night, ~zero on a mild one), and self-zeroing/off when unlearned or capped
to 0. Since v1.51.0 the margin moves the gate only; the radiators aim at comfort.
"""

from custom_components.scout_hut_heating.preheat import hold_margin
from scout_testkit import PRESET_COMFORT, PRESET_ICE, ZA, ZB, booking, hall_temp, make_controller, motion, preheat_window


# --- Pure maths -------------------------------------------------------------
def _m(**kw):
    # 20.625 = the old 15 in the learned-allowance units (x1.375, v1.51.0).
    base = dict(comfort=19.5, outdoor=5.0, cool_k=0.10, warmup_rate=20.625, cap=1.5)
    base.update(kw)
    return hold_margin(**base)


def test_cold_night_earns_a_positive_margin():
    # gap 14.5, cool 1.45/h, lead 20*20.625/16.5=25 min -> 0.604 C
    assert round(_m(), 3) == 0.604


def test_mild_night_earns_almost_nothing():
    assert _m(outdoor=19.0) < 0.05  # gap 0.5 -> tiny


def test_outdoor_at_or_above_comfort_is_zero():
    assert _m(outdoor=19.5) == 0.0
    assert _m(outdoor=25.0) == 0.0


def test_sluggish_hall_gets_a_bigger_margin_than_a_brisk_one():
    assert _m(warmup_rate=20.0) > _m(warmup_rate=10.0)


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
    ctrl._numbers["zone_a_warmup_rate"].native_value = 20.625
    ctrl._numbers["zone_a_warmup_rate_fans"].native_value = 20.625
    booking(ctrl, ZA)
    return ctrl, hass


def test_running_cold_booking_has_a_hold_margin():
    ctrl, _ = _cold_evening_booking()
    assert ctrl._booking_hold_margin(ZA) > 0.0
    # ...which the gate uses, but the radiators aim at comfort (v1.51.0).
    assert ctrl._drive_comfort_target(ZA) == ctrl.number("hall_comfort_temp")


def _hold_evening(outdoor):
    # 2026-10-22: comfort 19, hall k 9.65, gain 11.67.
    ctrl, hass = make_controller()
    hass.states.set("weather.forecast", "cloudy", {"temperature": outdoor})
    ctrl._numbers["hall_comfort_temp"].native_value = 19.0
    ctrl._numbers["zone_a_heatloss_pct"].native_value = 9.65
    # 11.67 in the units of the day (fixed 2.5 allowance), x1.375 since v1.51.0.
    ctrl._numbers["zone_a_warmup_rate"].native_value = 16.05
    ctrl._numbers["zone_a_warmup_rate_fans"].native_value = 16.05
    booking(ctrl, ZA)
    return ctrl, hass


def test_a_hold_under_half_a_degree_adds_nothing_at_the_radiators():
    # The hold was 0.28 at outdoor 10 and 0.36 at 7.4. Rounded to nearest both
    # became 19.5, the 0.5 overdrive went on top, and every hall radiator was
    # set to 20.0: the bookings peaked at 20.62 and 20.12 (v1.50.6).
    for outdoor in (10.0, 7.4):
        ctrl, _ = _hold_evening(outdoor)
        assert 0.25 < ctrl._booking_hold_margin(ZA) < 0.5
        assert ctrl._drive_comfort_target(ZA) == 19.0


def test_no_hold_reaches_the_radiators():
    # 2026-10-30: k 18.63 after two post-session cool-offs, outdoor 9.9, margin
    # 0.55. Rounded down it was still a whole 0.5, the hall radiators went to
    # 20.0 and the Friday booking ran more than 0.5 over for 129 min (v1.51.0).
    ctrl, _ = _hold_evening(9.9)
    ctrl._numbers["zone_a_heatloss_pct"].native_value = 18.63
    ctrl._numbers["zone_a_warmup_rate_fans"].native_value = 16.14
    ctrl._numbers["zone_a_warmup_rate"].native_value = 16.14
    assert ctrl._booking_hold_margin(ZA) > 0.5
    assert ctrl._drive_comfort_target(ZA) == 19.0
    # The gate still holds the zone in comfort above bare comfort.
    motion(ctrl, "hall")
    hall_temp(ctrl, 19.5)
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT


def test_the_heating_still_engages_on_the_raw_hold():
    # The second booking relit at an average of 19.25: above bare comfort, under
    # the held 19.36. Rounding the radiators' target down leaves that alone.
    ctrl, _ = _hold_evening(7.4)
    motion(ctrl, "hall")
    hall_temp(ctrl, 19.25)
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT
    ctrl._numbers["booking_hold_cap"].native_value = 0
    assert ctrl._desired_zone(ZA) == PRESET_ICE


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
    ctrl.cal_title[ZA] = "test"  # ECO keyword -> aims at eco, not comfort
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
