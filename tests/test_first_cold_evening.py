"""The first cold-soaked evening on v1.51.0 (2026-11-02, 18:30Z booking).

The hall started its pre-heat at 12.75 °C and the kitchen and toilets at
11-12, outdoor 8-10. Everything arrived short: the hall 0.62 (average 18.38),
the kitchen 17.5, the ladies 17.0 and the gents 16.5 against 19. Three things
in our control made it worse, and these tests pin each fix (v1.51.1):

* an easing cut the hall's elements at 18.5 in the last 15 min of the
  pre-heat, because one leader arriving early made the empty room "occupied";
* every climb read slower than the gain it had been led on, and the EWMA only
  moved each gain 30 % of the way toward it;
* a room still in the comfort it entered for its pre-heat opened a fresh
  warm-up sample the moment the booking ended.

Plus the measurement that was missing: which room plateaued, and whether its
radiator was still firing.
"""

import pytest

from custom_components.scout_hut_heating.coordinator import DRIVE_COAST_ALLOWANCE
from custom_components.scout_hut_heating.preheat import required_lead_minutes, updated_rate
from scout_testkit import (
    E,
    PRESET_COMFORT,
    ZA,
    advance,
    booking,
    make_controller,
    motion,
    run,
    set_registry,
)

HB = "climate.hall_back"
HB_SURFACE = "sensor.hall_back_surface_temperature"
KITCHEN, GENTS, LADIES = E["shared"]


def _hall_with_hot_panels(panel=74.0):
    ctrl, hass = make_controller()
    ctrl._numbers["hall_comfort_temp"].native_value = 19.0
    set_registry(
        entries_by_device={"dev_hall_back": [HB_SURFACE]},
        entity_devices={HB: "dev_hall_back"},
    )
    hass.states.set(HB_SURFACE, str(panel))
    return ctrl, hass


def _coast(ctrl, zone_avg):
    return ctrl._drive_coast(ZA, 19.0, zone_avg, True, ctrl._now())


def _events(ctrl, name):
    return [e for e in ctrl.audit.to_list() if e.get("event") == name]


# --- No easing in a pre-heat that has not arrived --------------------------------
def test_a_pre_heat_still_short_is_not_eased_even_with_someone_in():
    # 18:15Z: pre-heat window open, booking at 18:30Z, a leader in the hall since
    # 18:02Z, the average 18.5 on 74 °C panels.
    ctrl, _ = _hall_with_hot_panels()
    ctrl.cal_window[ZA] = True
    motion(ctrl, "hall")
    assert _coast(ctrl, 18.5) == 0.0
    assert _events(ctrl, "drive_coast_ease") == []


def test_once_the_booking_has_started_it_eases_as_before():
    ctrl, _ = _hall_with_hot_panels()
    ctrl.cal_window[ZA] = True
    booking(ctrl, ZA)
    motion(ctrl, "hall")
    assert _coast(ctrl, 18.5) == DRIVE_COAST_ALLOWANCE


def test_a_pre_heat_that_has_arrived_still_eases():
    # 2026-10-30 17:04Z: a pre-heat at target eased and the panels carried it
    # +0.62, the tail the easing is for.
    ctrl, _ = _hall_with_hot_panels(58.0)
    ctrl.cal_window[ZA] = True
    assert _coast(ctrl, 19.0) == DRIVE_COAST_ALLOWANCE


# --- A slow climb is believed at once --------------------------------------------
def test_the_hall_climb_moves_the_gain_all_the_way():
    # 16:00 -> 18:30Z, cut short at the booking: 12.75 -> 18.38 in 149.55 min,
    # average gap 7.5, k 8.68 %/h, read 18.13 against the 16.15 it was led on.
    new = updated_rate(16.15, 149.55, 5.62, 7.5, 0.0868)
    assert new == pytest.approx(18.13, abs=0.02)
    # The same evening's lead on it: ~20 min longer than the 154 it got.
    old_lead = required_lead_minutes(
        rate=16.15, indoor=12.75, target=19.0, outdoor=8.8, max_minutes=240,
        gap_hours=0, cool_k=0.0868,
    )
    new_lead = required_lead_minutes(
        rate=new, indoor=12.75, target=19.0, outdoor=8.8, max_minutes=240,
        gap_hours=0, cool_k=0.0868,
    )
    assert new_lead - old_lead >= 18


def test_the_gents_climb_moves_its_gain_as_far_as_the_cap():
    # 11.0 -> 16.5 in 194 min read 26.5 min/°C against the seed 20: 25 (the
    # 25 % step cap), where the old EWMA stopped at 21.96.
    assert updated_rate(20.0, 194.24, 5.5, 5.43, 0.0469) == pytest.approx(25.0)


def test_a_fast_climb_still_moves_only_part_of_the_way():
    # The cold-arrival direction keeps the EWMA: 18 -> 12 reads move 30 %.
    assert updated_rate(18.0, 60, 4) == pytest.approx(18.0 + 0.3 * (12.0 - 18.0))


# --- No sample after the booking it pre-heated for -------------------------------
def _gents_in_preheat_comfort(ctrl, hass, live_reason):
    hass.states.set(E["weather"], "cloudy", {"temperature": 7.0})
    hass.states.set(GENTS, "heat", {"current_temperature": 17.5})
    ctrl._numbers["shared_comfort_temp"].native_value = 19.0
    ctrl.applied["gents"] = PRESET_COMFORT
    ctrl._preset_entry_reason["gents"] = "preheat"
    ctrl._preset_entered_at["gents"] = ctrl._now()
    ctrl._preset_reason["gents"] = live_reason


def test_no_sample_opens_when_the_booking_has_ended():
    # 20:00Z: the booking ended with the gents still in the comfort it entered
    # at 15:15Z for the pre-heat; a sample opened and closed 18 s later.
    ctrl, hass = make_controller()
    _gents_in_preheat_comfort(ctrl, hass, "booking")
    ctrl._update_warmup_learning()
    assert ctrl._warmup_start["gents"] is None


def test_a_sample_still_opens_on_a_real_pre_heat():
    ctrl, hass = make_controller()
    _gents_in_preheat_comfort(ctrl, hass, "preheat")
    ctrl._update_warmup_learning()
    assert ctrl._warmup_start["gents"] is not None


def test_a_restart_mid_pre_heat_opens_no_sample_on_a_room_already_heating():
    # The restart re-applies comfort; inside the 25-min startup grace no sample
    # opens, and once it has passed the climb is half done, the panels hot.
    ctrl, hass = make_controller()
    _gents_in_preheat_comfort(ctrl, hass, "preheat")
    ctrl.applied["gents"] = None
    run(ctrl._async_set_preset("gents", PRESET_COMFORT))
    assert ctrl._preset_entry_reason["gents"] == "preheat"
    advance(ctrl, 26)
    ctrl._update_warmup_learning()
    assert ctrl._warmup_start["gents"] is None


def test_a_sample_opens_on_the_tick_after_the_pre_heat_starts():
    ctrl, hass = make_controller()
    _gents_in_preheat_comfort(ctrl, hass, "preheat")
    ctrl.applied["gents"] = None
    run(ctrl._async_set_preset("gents", PRESET_COMFORT))
    advance(ctrl, 1)
    ctrl._update_warmup_learning()
    assert ctrl._warmup_start["gents"] is not None


# --- The jump audit stays on zones being driven ----------------------------------
def test_a_jump_on_a_zone_not_being_driven_is_not_audited_but_still_settles():
    ctrl, _ = make_controller()
    now = ctrl._now()
    ctrl._note_coast_jump(ZA, 15.0, now, comfort=False)
    assert ctrl._note_coast_jump(ZA, 17.0, now, comfort=False) is True  # settling
    assert _events(ctrl, "drive_coast_jump") == []
    ctrl._note_coast_jump(ZA, 18.5, now, comfort=True)
    assert len(_events(ctrl, "drive_coast_jump")) == 1


# --- Each room in the trace ------------------------------------------------------
def test_the_trace_carries_each_room_and_its_panel():
    ctrl, hass = make_controller()
    for climate, temp in ((KITCHEN, 17.5), (GENTS, 16.5), (LADIES, 17.0)):
        hass.states.set(climate, "heat", {"current_temperature": temp})
    ctrl._sample_trace()
    point = ctrl.trace.to_list()[-1]
    assert (point["kitchen"], point["gents"], point["ladies"]) == (17.5, 16.5, 17.0)
    assert point["shared"] == 16.5  # still the coldest of the three
