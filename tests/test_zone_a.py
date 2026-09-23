"""Zone A (Hall) desired-preset priority table."""

from datetime import timedelta

from custom_components.scout_hut_heating.coordinator import BOOKING_RELIGHT_STALE_MIN
from scout_testkit import (
    PRESET_COMFORT,
    PRESET_ECO,
    PRESET_ICE,
    ZA,
    advance,
    booking,
    boost,
    hall_temp,
    make_controller,
    motion,
    on,
    E,
)


def test_empty_building_is_ice():
    ctrl, _ = make_controller()
    assert ctrl._desired_zone(ZA) == PRESET_ICE


# --- Heat/cool regime commit (stops a tight comfort/cooling gap hunting) ------


def test_cooling_hold_defers_heat_on_a_brief_between_games_dip():
    # Just cooled; an occupied hall dips only marginally below comfort (a rest
    # between games) -> held on ice, not flipped straight to heating.
    ctrl, _ = make_controller()
    motion(ctrl, "hall")
    hall_temp(ctrl, 19.0)  # comfort 19.5: wants heat, but only 0.5 below
    ctrl._hall_cooling_until = ctrl._now() + timedelta(minutes=10)
    assert ctrl._desired_zone(ZA) == PRESET_ICE  # cooling_hold


def test_cooling_hold_yields_to_a_genuine_cold_drop():
    # A real drop (> 1.5 below comfort) overrides the commit and heats.
    ctrl, _ = make_controller()
    motion(ctrl, "hall")
    hall_temp(ctrl, 17.5)  # 2.0 below comfort 19.5 -> games really over
    ctrl._hall_cooling_until = ctrl._now() + timedelta(minutes=10)
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT


def test_cooling_hold_expires_after_the_dwell():
    ctrl, _ = make_controller()
    motion(ctrl, "hall")
    hall_temp(ctrl, 19.0)
    ctrl._hall_cooling_until = ctrl._now() - timedelta(minutes=1)  # dwell elapsed
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT


def test_occupied_override_beats_the_cooling_hold():
    ctrl, _ = make_controller()
    ctrl._switches["zone_a_occupied_override"].is_on = True
    hall_temp(ctrl, 19.0)
    ctrl._hall_cooling_until = ctrl._now() + timedelta(minutes=10)
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT


# --- ...and the same commit now covers a BOOKING (field 2026-09-17) ----------
# The breeze reads a head-height MIX while the heat gate reads the COLDEST probe,
# so a cooling hall can read "wants heat" at the cold end while the people are
# still warm. Uncovered, that relit a booked hall mid-breeze: two reversals and a
# re-heat inside a session that was too warm throughout.


def test_cooling_hold_defers_a_booking_relight_mid_breeze():
    ctrl, _ = make_controller()
    booking(ctrl, ZA)
    motion(ctrl, "hall")
    hall_temp(ctrl, 19.0)  # coldest 0.5 below comfort: the gate wants heat...
    ctrl._hall_cooling_until = ctrl._now() + timedelta(minutes=10)
    assert ctrl._desired_zone(ZA) == PRESET_ICE  # ...but the hall is being cooled
    assert ctrl._preset_reason[ZA] == "cooling_hold"


def test_a_booked_hall_heats_again_once_the_breeze_has_let_go():
    ctrl, _ = make_controller()
    booking(ctrl, ZA)
    motion(ctrl, "hall")
    hall_temp(ctrl, 19.0)
    ctrl._hall_cooling_until = ctrl._now() - timedelta(minutes=1)  # dwell elapsed
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT


def test_a_genuinely_cold_booked_hall_overrides_the_cooling_hold():
    ctrl, _ = make_controller()
    booking(ctrl, ZA)
    motion(ctrl, "hall")
    hall_temp(ctrl, 17.5)  # 2.0 below comfort 19.5 — the breeze has gone too far
    ctrl._hall_cooling_until = ctrl._now() + timedelta(minutes=10)
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT


def test_occupied_override_beats_the_cooling_hold_on_a_booking_too():
    ctrl, _ = make_controller()
    booking(ctrl, ZA)
    ctrl._switches["zone_a_occupied_override"].is_on = True
    hall_temp(ctrl, 19.0)
    ctrl._hall_cooling_until = ctrl._now() + timedelta(minutes=10)
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT


def test_an_eco_booking_is_judged_against_its_own_low_goal():
    # A hall warm enough to be having a breeze is far above an eco-low 14, so it
    # lands on booking_warm before the hold is even reached — an eco session can
    # never relight heat mid-breeze, whichever gate you look at.
    ctrl, _ = make_controller()
    booking(ctrl, ZA, "sal-vation cleaning")
    motion(ctrl, "hall")
    hall_temp(ctrl, 19.0)
    ctrl._hall_cooling_until = ctrl._now() + timedelta(minutes=10)
    assert ctrl._desired_zone(ZA) == PRESET_ICE
    assert ctrl._preset_reason[ZA] == "booking_warm"


def test_booking_with_motion_is_comfort():
    ctrl, _ = make_controller()
    booking(ctrl, ZA)
    motion(ctrl, "hall")
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT


def test_booking_without_motion_drops_to_eco():
    # Genuinely empty booking (no motion, no override, not night-armed) -> eco.
    ctrl, _ = make_controller()
    booking(ctrl, ZA)
    assert ctrl._desired_zone(ZA) == PRESET_ECO


def test_sleepover_override_holds_a_booking_at_comfort():
    # A sleepover: people present but STILL, so the hall PIR sees no motion. The
    # occupied override is the "we're here" signal -> booking_quiet must NOT demote.
    ctrl, _ = make_controller()
    booking(ctrl, ZA)
    hall_temp(ctrl, 17.0)  # cold, genuinely wants heat
    ctrl._switches["zone_a_occupied_override"].is_on = True
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT
    assert ctrl._preset_reason[ZA] == "booking"


def test_sleepover_night_alarm_holds_a_booking_at_comfort():
    ctrl, hass = make_controller()
    booking(ctrl, ZA)
    hall_temp(ctrl, 17.0)
    hass.states.set(E["alarm_main"], "armed_night")  # people sleeping inside
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT


def test_eco_keyword_booking_stays_eco_even_with_motion():
    ctrl, _ = make_controller()
    booking(ctrl, ZA, "Test event")  # 'test' is a default ECO keyword
    motion(ctrl, "hall")
    assert ctrl._desired_zone(ZA) == PRESET_ECO


def test_opening_ice_forces_ice_over_booking():
    ctrl, _ = make_controller()
    booking(ctrl, ZA)
    motion(ctrl, "hall")
    ctrl.opening_ice[ZA] = True
    assert ctrl._desired_zone(ZA) == PRESET_ICE


def test_boost_beats_seasonal_lockout():
    ctrl, _ = make_controller()
    ctrl.seasonal_lockout = True
    boost(ctrl, ZA)
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT


def test_season_no_longer_ices_a_cold_booking():
    # The season no longer gates heating: a cold booking heats whatever the
    # calendar says (the old lockout would have forced ice here).
    ctrl, _ = make_controller()
    ctrl.seasonal_lockout = True
    booking(ctrl, ZA)
    motion(ctrl, "hall")
    hall_temp(ctrl, 12.0)  # genuinely cold
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT


def test_warm_booking_ices_for_the_cooling_fans():
    # A booking already at/above target needs no heat; ice frees the cooling fans.
    ctrl, hass = make_controller()
    hass.states.set("weather.forecast", "sunny", {"temperature": 22.0})  # warm -> no hold
    booking(ctrl, ZA)
    motion(ctrl, "hall")
    hall_temp(ctrl, 20.5)  # above the 19.5 comfort target
    assert ctrl._desired_zone(ZA) == PRESET_ICE
    assert ctrl._preset_reason[ZA] == "booking_warm"


def test_automation_disabled_leaves_alone():
    ctrl, _ = make_controller()
    ctrl._switches["zone_a_automation_enabled"].is_on = False
    booking(ctrl, ZA)
    assert ctrl._desired_zone(ZA) is None


def test_manual_hold_leaves_alone():
    ctrl, _ = make_controller()
    ctrl.manual_hold[ZA] = True
    booking(ctrl, ZA)
    assert ctrl._desired_zone(ZA) is None


def test_alarm_without_booking_is_ice():
    ctrl, hass = make_controller()
    on(hass, E["alarm_main"])
    assert ctrl._desired_zone(ZA) == PRESET_ICE


def test_alarm_during_booking_still_heats():
    ctrl, hass = make_controller()
    on(hass, E["alarm_main"])
    booking(ctrl, ZA)
    motion(ctrl, "hall")
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT


def test_motion_in_a_cold_hall_heats_to_comfort():
    # Unified with a booking: bare presence in a genuinely cold hall heats to the
    # SAME comfort target (was eco 16 before the occupied/booked split was removed).
    ctrl, _ = make_controller()
    motion(ctrl, "hall")
    hall_temp(ctrl, 15.0)
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT
    assert ctrl._preset_reason[ZA] == "motion"


def test_motion_in_a_warm_hall_ices_for_the_cooling_fans():
    ctrl, _ = make_controller()
    motion(ctrl, "hall")
    hall_temp(ctrl, 21.0)  # already warm — no heat, let the fans cool
    assert ctrl._desired_zone(ZA) == PRESET_ICE
    assert ctrl._preset_reason[ZA] == "occupied_warm"


def test_occupied_override_in_a_cold_hall_heats_to_comfort():
    ctrl, _ = make_controller()
    ctrl._switches["zone_a_occupied_override"].is_on = True
    hall_temp(ctrl, 15.0)
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT
    assert ctrl._preset_reason[ZA] == "occupied_override"


def test_someone_elsewhere_rests_hall_at_eco():
    # The hall itself is quiet, but the building is not empty: rest at eco
    # rather than leaving a stale (possibly comfort) preset running.
    ctrl, _ = make_controller()
    motion(ctrl, "office")  # not the hall
    assert ctrl._desired_zone(ZA) == PRESET_ECO


def test_preheat_window_holds_comfort_while_empty():
    # The pre-heat window exists to reach the comfort target by event start:
    # the empty-room demotion applies only once the event is running.
    ctrl, _ = make_controller()
    ctrl.cal_window[ZA] = True  # event within pre-heat window, not yet started
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT


# --- A frozen probe must not hold a booked zone on ice ------------------------
def _iced_as_warm_in_window(ctrl, minutes_flat, temp=19.0):
    """Hall iced on booking_warm inside a pre-heat window, coldest probe at
    `temp` (default exactly the 19.0 target) and unchanged for `minutes_flat`
    (field 2026-09-22 07:48-08:46Z)."""
    ctrl._numbers["hall_comfort_temp"].native_value = 19.0
    ctrl.cal_window[ZA] = True
    hall_temp(ctrl, temp)
    ctrl._track_probe_changes()
    advance(ctrl, minutes_flat)
    ctrl.applied[ZA] = PRESET_ICE
    ctrl._preset_reason[ZA] = "booking_warm"


def test_a_probe_frozen_at_target_relights_the_preheat():
    # All four hall probes sat at 19.0 for 20+ min while the room cooled to
    # 18.0; on "coldest < target" alone the relight waited for a restart and
    # came 14 min before the booking. Past the stale window the zone relights.
    ctrl, _ = make_controller()
    _iced_as_warm_in_window(ctrl, BOOKING_RELIGHT_STALE_MIN + 1)
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT
    assert ctrl._preset_reason[ZA] == "preheat_stale"


def test_a_reading_that_moved_recently_keeps_the_warm_zone_on_ice():
    ctrl, _ = make_controller()
    _iced_as_warm_in_window(ctrl, BOOKING_RELIGHT_STALE_MIN - 5)
    assert ctrl._desired_zone(ZA) == PRESET_ICE
    assert ctrl._preset_reason[ZA] == "booking_warm"


def test_the_stale_relight_covers_a_running_booking_too():
    ctrl, _ = make_controller()
    _iced_as_warm_in_window(ctrl, BOOKING_RELIGHT_STALE_MIN + 1)
    booking(ctrl, ZA)
    motion(ctrl, "hall")
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT
    assert ctrl._preset_reason[ZA] == "booking_stale"


def test_once_relit_the_zone_holds_comfort_until_the_reading_is_refreshed():
    # The next tick sees comfort applied on the SAME stale reading: the relight
    # holds (v1.45.1) rather than being re-judged warm and iced again.
    ctrl, _ = make_controller()
    _iced_as_warm_in_window(ctrl, BOOKING_RELIGHT_STALE_MIN + 1)
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT
    ctrl.applied[ZA] = PRESET_COMFORT
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT
    assert ctrl._preset_reason[ZA] == "preheat_stale"


def test_a_stale_reading_far_above_target_is_not_relit():
    # Field 2026-09-22 16:30Z: a sal-vation eco booking (target 14) on a hall
    # reading 22.5 that had sat 40-130 min was relit — no room loses 8 °C in
    # two hours. The relight now needs the fall to be physically possible.
    ctrl, hass = make_controller()
    ctrl._numbers["zone_a_heatloss_pct"].native_value = 11.0  # the learned hall k
    hass.states.set(E["weather"], "cloudy", {"temperature": 20.0})
    _iced_as_warm_in_window(ctrl, BOOKING_RELIGHT_STALE_MIN + 40, temp=22.5)
    assert ctrl._desired_zone(ZA) == PRESET_ICE
    assert ctrl._preset_reason[ZA] == "booking_warm"
    # Even with the outdoor unknown (the cold fallback errs warm) 22.5 cannot
    # have reached 19 in an hour at the learned loss rate.
    hass.states.set(E["weather"], "cloudy", {})
    assert ctrl._desired_zone(ZA) == PRESET_ICE


def test_a_stale_relight_above_the_release_band_does_not_flap():
    # Field 2026-09-22 16:30-16:35Z and 08:56-09:00Z the next morning: relit
    # on a stale reading above target + 0.5, re-judged warm on the same value
    # the next tick, iced, relit — a heater write pair every tick. The relight
    # holds until the reading actually changes; then the ordinary gate ices it
    # and the flat clock has restarted, so it is not immediately relit again.
    ctrl, hass = make_controller()
    hass.states.set(E["weather"], "cloudy", {"temperature": 5.0})
    _iced_as_warm_in_window(ctrl, BOOKING_RELIGHT_STALE_MIN + 40, temp=20.0)
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT
    assert ctrl._preset_reason[ZA] == "preheat_stale"
    ctrl.applied[ZA] = PRESET_COMFORT
    for _ in range(3):  # ticks on the same stale reading: held, not iced
        ctrl._track_probe_changes()
        assert ctrl._desired_zone(ZA) == PRESET_COMFORT
        assert ctrl._preset_reason[ZA] == "preheat_stale"
    hall_temp(ctrl, 20.5)  # a fresh reading arrives, still warm
    ctrl._track_probe_changes()
    assert ctrl._desired_zone(ZA) == PRESET_ICE
    assert ctrl._preset_reason[ZA] == "booking_warm"
    ctrl.applied[ZA] = PRESET_ICE
    assert ctrl._desired_zone(ZA) == PRESET_ICE  # flat clock restarted: no relight


def test_an_occupied_stale_relight_holds_too():
    ctrl, hass = make_controller()
    hass.states.set(E["weather"], "cloudy", {"temperature": 5.0})
    ctrl._numbers["hall_comfort_temp"].native_value = 19.0
    motion(ctrl, "hall")
    hall_temp(ctrl, 20.0)
    ctrl._track_probe_changes()
    advance(ctrl, BOOKING_RELIGHT_STALE_MIN + 40)
    motion(ctrl, "hall")
    ctrl.applied[ZA] = PRESET_ICE
    ctrl._preset_reason[ZA] = "occupied_warm"
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT
    assert ctrl._preset_reason[ZA] == "occupied_stale"
    ctrl.applied[ZA] = PRESET_COMFORT
    ctrl._track_probe_changes()
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT
    assert ctrl._preset_reason[ZA] == "occupied_stale"


def test_an_occupied_zone_iced_as_warm_relights_on_a_stale_reading_too():
    # Motion-only heating ices on `occupied_warm` on the same coldest probe, so
    # a frozen reading at target would hold people in a cooling room. Same rule.
    ctrl, _ = make_controller()
    ctrl._numbers["hall_comfort_temp"].native_value = 19.0
    motion(ctrl, "hall")
    hall_temp(ctrl, 19.0)
    ctrl._track_probe_changes()
    advance(ctrl, BOOKING_RELIGHT_STALE_MIN + 1)
    motion(ctrl, "hall")  # still here
    ctrl.applied[ZA] = PRESET_ICE
    ctrl._preset_reason[ZA] = "occupied_warm"
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT
    assert ctrl._preset_reason[ZA] == "occupied_stale"


def test_an_occupied_zone_with_a_fresh_reading_at_target_stays_on_ice():
    ctrl, _ = make_controller()
    ctrl._numbers["hall_comfort_temp"].native_value = 19.0
    motion(ctrl, "hall")
    hall_temp(ctrl, 19.0)
    ctrl._track_probe_changes()
    ctrl.applied[ZA] = PRESET_ICE
    ctrl._preset_reason[ZA] = "occupied_warm"
    assert ctrl._desired_zone(ZA) == PRESET_ICE
    assert ctrl._preset_reason[ZA] == "occupied_warm"


def test_a_zone_iced_for_another_reason_is_not_relit_by_staleness():
    # Only a `booking_warm` ice is a reading-held decision. A zone that was
    # iced for some other reason is judged on the ordinary gate, where a
    # reading exactly at target is "warm enough" — and lands on booking_warm.
    ctrl, _ = make_controller()
    _iced_as_warm_in_window(ctrl, BOOKING_RELIGHT_STALE_MIN + 1)
    ctrl._preset_reason[ZA] = "cooling_hold"
    assert ctrl._desired_zone(ZA) == PRESET_ICE
    assert ctrl._preset_reason[ZA] == "booking_warm"


def test_alarm_clears_the_occupied_override():
    # Original A33/A34: arming with no booking cancels a lingering override,
    # or it would silently resume heating the empty zone at disarm.
    ctrl, hass = make_controller()
    ctrl._switches["zone_a_occupied_override"].is_on = True
    on(hass, E["alarm_main"])
    assert ctrl._desired_zone(ZA) == PRESET_ICE
    assert ctrl.switch_on("zone_a_occupied_override") is False
