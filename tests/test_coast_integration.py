"""The coast predictor wired into the ladder (the `coast_when_free` switch).

During the HALL pre-heat window, a room measurably warming on free gain fast
enough to reach the comfort band by event start is held at eco (reason
`preheat_coast`) instead of firing the radiators. Hall-only, pre-heat-only,
comfort-lean, off by default, and the passive rise is measured only while the
heaters are idle.
"""

from datetime import timedelta

from scout_testkit import (
    PRESET_COMFORT,
    PRESET_ECO,
    ZA,
    ZB,
    E,
    booking,
    make_controller,
    preheat_window,
)


def _hall_temp(ctrl, temp):
    for eid in E["hall"]:
        ctrl.hass.states.set(eid, "heat", {"current_temperature": temp})


def _office_temp(ctrl, temp):
    for eid in E["office"]:
        ctrl.hass.states.set(eid, "heat", {"current_temperature": temp})


def _seed_rise(ctrl, indoor, rate_c_per_min, span_min=15.0):
    """Seed the idle-room buffer so `_passive_rise_rate()` reads `rate`.

    Two samples `span_min` apart whose newest value equals `indoor` (the
    current coldest reading the predictor also reads directly).
    """
    now = ctrl._now()
    start = indoor - rate_c_per_min * span_min
    ctrl._passive_rise.clear()
    ctrl._passive_rise.append((now - timedelta(minutes=span_min), start))
    ctrl._passive_rise.append((now, indoor))


def _preheat(ctrl, gap_min=120.0):
    """Put the hall in a pre-heat window (event not yet running) with a gap."""
    preheat_window(ctrl, ZA)
    ctrl._last_lead_calc[ZA] = {"gap_min": gap_min}


def _on(ctrl):
    ctrl._switches["coast_when_free"].is_on = True


# --- Off by default --------------------------------------------------------
def test_off_by_default_preheats_normally():
    ctrl, _ = make_controller()
    _preheat(ctrl)
    _hall_temp(ctrl, 17.0)
    _seed_rise(ctrl, 17.0, 0.1)  # would coast if the switch were on
    assert not ctrl._switches["coast_when_free"].is_on
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT
    assert ctrl._preset_reason[ZA] == "preheat"


# --- The coast decision ----------------------------------------------------
def test_warming_hall_coasts_to_eco():
    ctrl, _ = make_controller()
    _on(ctrl)
    _preheat(ctrl, gap_min=120)
    _hall_temp(ctrl, 17.0)
    _seed_rise(ctrl, 17.0, 0.1)  # 2.2 °C to band / 0.1 = 22 min * 1.3 << 120
    assert ctrl._desired_zone(ZA) == PRESET_ECO
    assert ctrl._preset_reason[ZA] == "preheat_coast"


def test_static_hall_preheats():
    ctrl, _ = make_controller()
    _on(ctrl)
    _preheat(ctrl, gap_min=120)
    _hall_temp(ctrl, 17.0)
    _seed_rise(ctrl, 17.0, 0.0)  # not warming -> heat
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT
    assert ctrl._preset_reason[ZA] == "preheat"


def test_warming_but_too_late_preheats():
    ctrl, _ = make_controller()
    _on(ctrl)
    _preheat(ctrl, gap_min=20)  # only 20 min left
    _hall_temp(ctrl, 17.0)
    _seed_rise(ctrl, 17.0, 0.05)  # 2.2/0.05 = 44 min, past the gap
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT


def test_coast_only_during_preheat_not_running_event():
    """Coast never fires once the event is running — normal booking rungs apply.

    (An unoccupied running booking lands on `booking_quiet` eco; the point is
    the reason is NOT `preheat_coast`, so a warming room did not suppress a live
    booking's heat.)
    """
    ctrl, _ = make_controller()
    _on(ctrl)
    booking(ctrl, ZA)  # event actually running
    ctrl._last_lead_calc[ZA] = {"gap_min": 0.0}
    _hall_temp(ctrl, 17.0)
    _seed_rise(ctrl, 17.0, 0.2)
    ctrl._desired_zone(ZA)
    assert ctrl._preset_reason[ZA] != "preheat_coast"
    assert ctrl._coasting[ZA] is False


def test_running_event_with_motion_heats_comfort_not_coast():
    ctrl, _ = make_controller()
    _on(ctrl)
    booking(ctrl, ZA)
    ctrl._last_lead_calc[ZA] = {"gap_min": 0.0}
    from scout_testkit import motion

    motion(ctrl, "hall")
    _hall_temp(ctrl, 17.0)
    _seed_rise(ctrl, 17.0, 0.2)
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT
    assert ctrl._preset_reason[ZA] == "booking"


def test_running_occupied_booking_coasts_when_warm_and_rising():
    """The 2026-08-05 case: occupied booking, room in band and climbing -> hold eco."""
    ctrl, _ = make_controller()
    _on(ctrl)
    booking(ctrl, ZA)
    from scout_testkit import motion

    motion(ctrl, "hall")
    _hall_temp(ctrl, 19.4)  # target 19.5, band 19.2 -> in band
    _seed_rise(ctrl, 19.4, 0.05)  # measurably rising on free gain
    assert ctrl._desired_zone(ZA) == PRESET_ECO
    assert ctrl._preset_reason[ZA] == "booking_coast"


def test_running_booking_below_band_still_heats():
    """Comfort-lean: an occupied room actually below comfort is never withheld."""
    ctrl, _ = make_controller()
    _on(ctrl)
    booking(ctrl, ZA)
    from scout_testkit import motion

    motion(ctrl, "hall")
    _hall_temp(ctrl, 18.0)  # well below the band
    _seed_rise(ctrl, 18.0, 0.05)  # rising, but not there yet and deadline is now
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT
    assert ctrl._preset_reason[ZA] == "booking"


def test_running_booking_in_band_but_flat_heats():
    ctrl, _ = make_controller()
    _on(ctrl)
    booking(ctrl, ZA)
    from scout_testkit import motion

    motion(ctrl, "hall")
    _hall_temp(ctrl, 19.4)
    _seed_rise(ctrl, 19.4, 0.0)  # not rising -> not coasting
    assert ctrl._desired_zone(ZA) == PRESET_COMFORT


def test_running_unoccupied_booking_is_quiet_not_coast():
    ctrl, _ = make_controller()
    _on(ctrl)
    booking(ctrl, ZA)  # running, no motion
    _hall_temp(ctrl, 19.4)
    _seed_rise(ctrl, 19.4, 0.05)
    assert ctrl._desired_zone(ZA) == PRESET_ECO
    assert ctrl._preset_reason[ZA] == "booking_quiet"


def test_office_preheat_never_coasts():
    """Coast is hall-only: an office pre-heat always heats."""
    ctrl, _ = make_controller()
    _on(ctrl)
    preheat_window(ctrl, ZB)
    ctrl._last_lead_calc[ZB] = {"gap_min": 120.0}
    _office_temp(ctrl, 17.0)
    _seed_rise(ctrl, 17.0, 0.2)  # buffer is the hall's, but the gate is hall-only anyway
    assert ctrl._desired_zone(ZB) == PRESET_COMFORT


# --- Passive-rise measurement ----------------------------------------------
def test_rate_none_without_enough_span():
    ctrl, _ = make_controller()
    now = ctrl._now()
    # Two samples only 5 min apart — below the min span.
    ctrl._passive_rise.append((now - timedelta(minutes=5), 16.0))
    ctrl._passive_rise.append((now, 17.0))
    assert ctrl._passive_rise_rate() is None


def test_rate_measured_over_window():
    ctrl, _ = make_controller()
    _seed_rise(ctrl, 18.0, 0.1, span_min=15.0)
    rate = ctrl._passive_rise_rate()
    assert rate is not None
    assert abs(rate - 0.1) < 1e-9


def test_demand_clears_the_buffer():
    """A tick with active heat demand wipes the idle-room samples."""
    ctrl, _ = make_controller()
    _seed_rise(ctrl, 18.0, 0.1)
    _hall_temp(ctrl, 18.0)
    ctrl.applied[ZA] = PRESET_COMFORT  # no power sensors mapped -> preset = demand
    ctrl._update_passive_rise()
    assert len(ctrl._passive_rise) == 0
    assert ctrl._passive_rise_rate() is None


def test_idle_tick_accumulates():
    ctrl, _ = make_controller()
    _hall_temp(ctrl, 18.0)
    ctrl.applied[ZA] = "ice"  # not a heating preset -> idle
    ctrl.applied[ZB] = "ice"
    before = len(ctrl._passive_rise)
    ctrl._update_passive_rise()
    assert len(ctrl._passive_rise) == before + 1


def test_coast_records_an_audit_event_on_entry():
    ctrl, _ = make_controller()
    _on(ctrl)
    _preheat(ctrl, gap_min=120)
    _hall_temp(ctrl, 17.0)
    _seed_rise(ctrl, 17.0, 0.1)
    ctrl._desired_zone(ZA)
    ctrl._desired_zone(ZA)  # second tick must NOT re-audit (latched)
    coast_events = [e for e in ctrl.audit._events if e.get("event") == "coast_decision"]
    assert len(coast_events) == 1
    assert coast_events[0]["zone"] == ZA
