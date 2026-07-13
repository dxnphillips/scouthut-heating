"""The audit log and the diagnostics export.

The learned-rate seeds and clamps were chosen from textbook figures, not from
the building. These tests lock in the machinery that lets real behaviour be
exported and analysed offline: every learning sample (accepted or rejected)
is recorded with its inputs, every pre-heat decision carries the numbers it
was computed from, and the booking-start outcome event captures the one
number that judges the whole learning stack — was the room warm on arrival.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from custom_components.scout_hut_heating.audit import AuditLog, Trace
from custom_components.scout_hut_heating.const import (
    CONF_FAN_MASTER,
    CONF_FAN_O1_POWER,
    DOMAIN,
)
from scout_testkit import (
    E,
    PRESET_COMFORT,
    PRESET_ECO,
    PRESET_ICE,
    ZA,
    advance,
    booking,
    make_controller,
    motion,
    run,
)


def _hall_temp(hass, temp):
    hass.states.set(E["hall"][0], "heat", {"current_temperature": temp})


def _set_rate(ctrl, key, value):
    ctrl._numbers[key].native_value = value


def events(ctrl, kind):
    return [e for e in ctrl.audit.to_list() if e["event"] == kind]


# --- The log itself ---------------------------------------------------------------

def test_audit_log_is_bounded_and_json_safe():
    from homeassistant.util import dt as dt_util

    log = AuditLog(maxlen=10)
    for i in range(25):
        log.record("tick", dt_util.utcnow(), n=i, ratio=1.23456, skipped=None)
    items = log.to_list()
    assert len(items) == 10
    assert items[-1]["n"] == 24  # oldest dropped, newest kept
    assert items[-1]["ratio"] == 1.23  # floats rounded
    assert "skipped" not in items[-1]  # None values dropped
    json.dumps(items)


def test_audit_log_restore_tolerates_bad_data():
    log = AuditLog()
    log.load("not a list")
    log.load([{"event": "ok"}, "junk", 42])
    assert [e["event"] for e in log.to_list()] == ["ok"]


# --- The readings trace -----------------------------------------------------------

def test_trace_throttles_to_the_sampling_interval():
    from datetime import datetime, timezone

    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    trace = Trace()
    assert trace.maybe_sample(t0, floor=18.34567, missing=None) is True
    assert trace.maybe_sample(t0 + timedelta(minutes=5), floor=19) is False
    assert trace.maybe_sample(t0 + timedelta(minutes=15), floor=19) is True

    points = trace.to_list()
    assert len(points) == 2
    assert points[0]["floor"] == 18.35  # rounded
    assert "missing" not in points[0]  # None dropped
    json.dumps(points)


def test_trace_is_bounded():
    from datetime import datetime, timezone

    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    trace = Trace(maxlen=4)
    for i in range(10):
        trace.maybe_sample(t0 + timedelta(minutes=15 * i), n=i)
    points = trace.to_list()
    assert len(points) == 4 and points[-1]["n"] == 9


def test_trace_keeps_its_cadence_across_a_restart():
    from datetime import datetime, timezone

    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    trace = Trace()
    trace.maybe_sample(t0, floor=18)

    restored = Trace()
    restored.load(trace.to_list())
    # One minute after the persisted point: still inside the interval.
    assert restored.maybe_sample(t0 + timedelta(minutes=1), floor=18) is False
    assert restored.maybe_sample(t0 + timedelta(minutes=16), floor=18) is True


def test_controller_trace_records_the_computed_readings():
    ctrl, hass = make_controller()
    _hall_temp(hass, 20)
    hass.states.set(E["weather"], "cloudy", {"temperature": 15})
    ctrl._sample_trace()
    ctrl._sample_trace()  # throttled: still one point

    (point,) = ctrl.trace.to_list()
    assert point["floor"] == 20.0
    assert point["hall_coldest"] == 20.0
    assert point["outdoor"] == 15.0
    assert point["fans"] is False

    snap = ctrl._state_snapshot()
    assert snap["trace"] == ctrl.trace.to_list()


# --- Learning samples -------------------------------------------------------------

def test_accepted_warmup_sample_is_audited_with_its_inputs():
    ctrl, hass = make_controller()
    _set_rate(ctrl, "zone_a_warmup_rate", 20)
    _hall_temp(hass, 18)
    ctrl.applied[ZA] = PRESET_COMFORT
    ctrl._update_warmup_learning()
    advance(ctrl, 120)
    _hall_temp(hass, 22)  # target reached after 120 min / 4 °C
    ctrl._update_warmup_learning()

    (evt,) = events(ctrl, "warmup_sample")
    assert evt["zone"] == ZA
    assert evt["rate_key"] == "zone_a_warmup_rate"
    assert evt["accepted"] is True
    assert evt["reached_target"] is True
    assert evt["minutes"] == pytest.approx(120, abs=0.1)
    assert evt["rise"] == pytest.approx(4.0)
    assert evt["old_rate"] == 20.0
    assert evt["new_rate"] == pytest.approx(23.0, abs=0.01)


def test_rejected_warmup_sample_is_audited_as_rejected():
    ctrl, hass = make_controller()
    _set_rate(ctrl, "zone_a_warmup_rate", 20)
    _hall_temp(hass, 18)
    ctrl.applied[ZA] = PRESET_COMFORT
    ctrl._update_warmup_learning()
    advance(ctrl, 20)
    _hall_temp(hass, 18.4)  # too small a rise to teach anything
    ctrl.applied[ZA] = PRESET_ECO  # warm-up ended early
    ctrl._update_warmup_learning()

    (evt,) = events(ctrl, "warmup_sample")
    assert evt["accepted"] is False
    assert evt["reached_target"] is False
    assert evt["new_rate"] == evt["old_rate"] == 20.0


def test_warmup_sample_records_the_average_fan_wattage():
    # The O1 wattage encodes the manual transformer dial tap; recording it
    # with each sample shows whether dial changes are perturbing the rates.
    ctrl, hass = make_controller(
        config_overrides={
            CONF_FAN_MASTER: "switch.fan_master",
            CONF_FAN_O1_POWER: "sensor.fan_power",
        }
    )
    hass.states.set("switch.fan_master", "on")
    hass.states.set("sensor.fan_power", "120.0")
    _set_rate(ctrl, "zone_a_warmup_rate_fans", 20)
    _hall_temp(hass, 18)
    ctrl.applied[ZA] = PRESET_COMFORT
    ctrl._update_warmup_learning()  # sample starts
    ctrl._update_warmup_learning()  # one mid-warm-up tick
    advance(ctrl, 120)
    _hall_temp(hass, 22)
    ctrl._update_warmup_learning()  # target reached

    (evt,) = events(ctrl, "warmup_sample")
    assert evt["rate_key"] == "zone_a_warmup_rate_fans"
    assert evt["o1_avg_w"] == pytest.approx(120.0)


def test_cooloff_sample_is_audited_with_its_gap():
    ctrl, hass = make_controller()
    _set_rate(ctrl, "zone_a_heatloss_pct", 20)
    hass.states.set(E["weather"], "cloudy", {"temperature": 10})
    _hall_temp(hass, 20)
    ctrl.applied[ZA] = PRESET_ICE
    ctrl._update_cooloff_learning()
    advance(ctrl, 240)
    _hall_temp(hass, 16)  # 4 °C over 4 h at an average gap of 8 -> k = 0.125/h
    ctrl._update_cooloff_learning()

    (evt,) = events(ctrl, "cooloff_sample")
    assert evt["zone"] == ZA
    assert evt["accepted"] is True
    assert evt["hours"] == pytest.approx(4.0, abs=0.01)
    assert evt["drop"] == pytest.approx(4.0)
    assert evt["gap"] == pytest.approx(8.0, abs=0.01)
    assert evt["old_pct"] == 20.0
    assert evt["new_pct"] == pytest.approx(17.75, abs=0.01)
    # Fans were off throughout: the sample says so.
    assert evt["fan_ticks"] == 0
    assert evt["ticks"] == 2


def test_cooloff_sample_records_a_fan_mixed_window():
    """A cool-off measured with the fans stirring the air is tagged as such.

    The 2026-07-11 sealed test showed mixing roughly halves the sealed hut's
    gap-normalised loss, and in winter the recirculation term runs the fans
    through the evening cool-off routinely — so the tally is the evidence a
    future fan-aware split of the constant would be tuned from.
    """
    ctrl, hass = make_controller(
        config_overrides={
            CONF_FAN_MASTER: "switch.fan_master",
            CONF_FAN_O1_POWER: "sensor.fan_power",
        }
    )
    _set_rate(ctrl, "zone_a_heatloss_pct", 20)
    hass.states.set(E["weather"], "cloudy", {"temperature": 10})
    hass.states.set("switch.fan_master", "on")
    hass.states.set("sensor.fan_power", "195.0")
    _hall_temp(hass, 20)
    ctrl.applied[ZA] = PRESET_ICE
    ctrl._update_cooloff_learning()  # anchors with fans already running
    advance(ctrl, 240)
    _hall_temp(hass, 16)
    ctrl._update_cooloff_learning()

    (evt,) = events(ctrl, "cooloff_sample")
    assert evt["accepted"] is True
    assert evt["fan_ticks"] == 2 and evt["ticks"] == 2  # mixed the whole way


def test_cooloff_sample_without_outdoor_is_rejected_not_guessed():
    ctrl, hass = make_controller()
    _set_rate(ctrl, "zone_a_heatloss_pct", 20)
    _hall_temp(hass, 20)  # no weather state at all
    ctrl.applied[ZA] = PRESET_ICE
    ctrl._update_cooloff_learning()
    advance(ctrl, 240)
    _hall_temp(hass, 16)
    ctrl._update_cooloff_learning()

    (evt,) = events(ctrl, "cooloff_sample")
    assert evt["accepted"] is False
    assert evt["reason"] == "no_outdoor"
    assert ctrl.number("zone_a_heatloss_pct") == 20  # unchanged


def test_motion_records_a_fresh_arrival():
    ctrl, _ = make_controller()
    motion(ctrl, "hall")

    (evt,) = events(ctrl, "motion")
    assert evt["area"] == "hall"


def test_repeated_motion_within_the_timeout_does_not_flood_the_log():
    # A PIR re-firing while the area is still occupied refreshes the timestamp
    # but must not append a second event — a busy session would otherwise push
    # the learning samples out of the bounded log.
    ctrl, _ = make_controller()
    motion(ctrl, "hall")
    advance(ctrl, 5)  # still within the 15-min occupancy timeout
    motion(ctrl, "hall")

    assert len(events(ctrl, "motion")) == 1


def test_motion_after_the_area_goes_quiet_is_a_new_arrival():
    ctrl, _ = make_controller()
    motion(ctrl, "hall")
    advance(ctrl, 20)  # longer than the 15-min timeout: the hall emptied
    motion(ctrl, "hall")

    assert len(events(ctrl, "motion")) == 2


def test_cooloff_sample_discarded_by_an_opening_is_audited():
    ctrl, hass = make_controller()
    _hall_temp(hass, 20)
    ctrl.applied[ZA] = PRESET_ICE
    ctrl._update_cooloff_learning()  # sample anchors
    assert ctrl._cooloff_start[ZA] is not None
    ctrl.opening_ice[ZA] = True
    ctrl._update_cooloff_learning()  # ventilation loss: discard

    (evt,) = events(ctrl, "cooloff_discarded")
    assert evt["zone"] == ZA and evt["reason"] == "opening"
    # The discard is recorded once, not on every subsequent tick.
    ctrl._update_cooloff_learning()
    assert len(events(ctrl, "cooloff_discarded")) == 1


# --- Pre-heat decisions and booking outcomes ---------------------------------------

def test_preheat_window_opening_records_the_decision_inputs(monkeypatch):
    from homeassistant.util import dt as dt_util

    ctrl, hass = make_controller()
    _set_rate(ctrl, "hall_comfort_temp", 22)  # pin: tests the recording, not the default
    _set_rate(ctrl, "zone_a_warmup_rate", 20)
    _set_rate(ctrl, "zone_a_heatloss_pct", 0)
    hass.states.set(E["weather"], "cloudy", {"temperature": 15})
    _hall_temp(hass, 19)  # 3 °C deficit x 20 min/°C -> 60 min lead
    start = dt_util.now() + timedelta(minutes=30)

    async def _events(cal, minutes):
        if cal == E["cal_hall"]:
            return [{"start": start.isoformat(), "summary": "Beavers"}]
        return []

    monkeypatch.setattr(ctrl, "_async_calendar_events", _events)
    run(ctrl._async_refresh_calendars())

    assert ctrl.cal_window[ZA] is True  # 30 min gap <= 60 min lead
    (evt,) = events(ctrl, "preheat_start")
    assert evt["zone"] == ZA
    assert evt["lead_min"] == 60
    assert evt["gap_min"] == pytest.approx(30, abs=0.1)
    assert evt["rate"] == 20.0
    assert evt["indoor_coldest"] == 19.0
    assert evt["target"] == 22.0
    assert evt["outdoor"] == 15.0

    # Staying inside the window on the next refresh records nothing new.
    run(ctrl._async_refresh_calendars())
    assert len(events(ctrl, "preheat_start")) == 1


def test_note_fan_speed_remembers_the_last_running_tap():
    """The last fan tap survives the master going off for a pre-heat idle gap.

    HA cannot command the transformer dial, and the master is off while a
    pre-heat waits — so this remembered value is the only record of the speed
    the optimistic fan-assisted lead is leaning on. A zero draw is the fans
    stopped, not a new (slower) setting, so it must not overwrite the memory.
    """
    ctrl, hass = make_controller(
        config_overrides={
            CONF_FAN_MASTER: "switch.fan_master",
            CONF_FAN_O1_POWER: "sensor.fan_power",
        }
    )
    hass.states.set("switch.fan_master", "on")
    hass.states.set("sensor.fan_power", "195.0")
    ctrl._note_fan_speed()
    assert ctrl._fan_w_last_seen == pytest.approx(195.0)

    hass.states.set("switch.fan_master", "off")
    hass.states.set("sensor.fan_power", "0.0")
    ctrl._note_fan_speed()
    assert ctrl._fan_w_last_seen == pytest.approx(195.0)  # idle gap: memory holds

    hass.states.set("switch.fan_master", "on")
    hass.states.set("sensor.fan_power", "110.0")
    ctrl._note_fan_speed()
    assert ctrl._fan_w_last_seen == pytest.approx(110.0)  # a real lower tap updates


def test_preheat_start_records_the_rate_key_and_assumed_fan_speed(monkeypatch):
    """The pre-heat decision carries which rate drove it and the fan tap it assumes.

    In winter with the fans expected to help, the lead is sized on the
    optimistic ``zone_a_warmup_rate_fans``; the tap that rate implicitly
    assumes is only knowable from the last time the fans ran, because the
    master is off through the idle gap. Both land on the event so a cold
    arrival can be read against a dialled-down speed (CLAUDE.md open Q14).
    """
    from homeassistant.util import dt as dt_util

    ctrl, hass = make_controller(
        config_overrides={
            CONF_FAN_MASTER: "switch.fan_master",
            CONF_FAN_O1_POWER: "sensor.fan_power",
        }
    )
    _set_rate(ctrl, "hall_comfort_temp", 22)
    _set_rate(ctrl, "zone_a_warmup_rate_fans", 20)
    _set_rate(ctrl, "zone_a_heatloss_pct", 0)
    hass.states.set(E["weather"], "cloudy", {"temperature": 15})
    _hall_temp(hass, 19)

    # The fans ran at 195 W earlier, then the master went off for the idle gap.
    hass.states.set("switch.fan_master", "on")
    hass.states.set("sensor.fan_power", "195.0")
    ctrl._note_fan_speed()
    hass.states.set("switch.fan_master", "off")

    start = dt_util.now() + timedelta(minutes=30)

    async def _events(cal, minutes):
        if cal == E["cal_hall"]:
            return [{"start": start.isoformat(), "summary": "Beavers"}]
        return []

    monkeypatch.setattr(ctrl, "_async_calendar_events", _events)
    run(ctrl._async_refresh_calendars())

    (evt,) = events(ctrl, "preheat_start")
    assert evt["rate_key"] == "zone_a_warmup_rate_fans"  # the optimistic path
    assert evt["fan_w_last"] == pytest.approx(195.0)


def test_hall_booking_start_records_the_assumed_fan_speed():
    ctrl, hass = make_controller(
        config_overrides={
            CONF_FAN_MASTER: "switch.fan_master",
            CONF_FAN_O1_POWER: "sensor.fan_power",
        }
    )
    _set_rate(ctrl, "hall_comfort_temp", 22)
    _hall_temp(hass, 20)
    hass.states.set("switch.fan_master", "on")
    hass.states.set("sensor.fan_power", "170.0")
    ctrl._note_fan_speed()
    hass.states.set("switch.fan_master", "off")

    ctrl._record_booking_edges()  # baseline observed: calendar off
    booking(ctrl, ZA, "Beavers")
    ctrl._record_booking_edges()

    (evt,) = events(ctrl, "booking_start")
    assert evt["shortfall"] == pytest.approx(2.0)
    assert evt["fan_w_last"] == pytest.approx(170.0)


def test_booking_start_outcome_records_the_arrival_shortfall():
    ctrl, hass = make_controller()
    _set_rate(ctrl, "hall_comfort_temp", 22)  # pin: tests the recording, not the default
    _hall_temp(hass, 20)
    ctrl._record_booking_edges()  # baseline observed: calendar off
    booking(ctrl, ZA, "Beavers")
    ctrl._record_booking_edges()

    (evt,) = events(ctrl, "booking_start")
    assert evt["zone"] == ZA
    assert evt["title"] == "beavers"
    assert evt["target"] == 22.0
    assert evt["coldest"] == 20.0
    assert evt["shortfall"] == pytest.approx(2.0)  # arrived 2 °C under target

    # No repeat while the same booking keeps running.
    ctrl._record_booking_edges()
    assert len(events(ctrl, "booking_start")) == 1


def test_booking_end_is_audited_with_the_leaving_temperature():
    from scout_testkit import end_booking

    ctrl, hass = make_controller()
    _hall_temp(hass, 20)
    ctrl._record_booking_edges()  # baseline
    booking(ctrl, ZA, "Beavers")
    ctrl._record_booking_edges()  # start
    _hall_temp(hass, 21.5)
    end_booking(ctrl, ZA)
    ctrl._record_booking_edges()  # end

    (evt,) = events(ctrl, "booking_end")
    assert evt["zone"] == ZA
    assert evt["coldest"] == 21.5
    # No repeat while the calendar stays off.
    ctrl._record_booking_edges()
    assert len(events(ctrl, "booking_end")) == 1


def test_restart_mid_booking_records_no_phantom_start():
    ctrl, hass = make_controller()
    booking(ctrl, ZA)
    ctrl._record_booking_edges()  # first observation IS the baseline
    assert not events(ctrl, "booking_start")


# --- Actuation events ---------------------------------------------------------------

def test_fan_state_changes_are_audited():
    ctrl, hass = make_controller(
        config_overrides={
            CONF_FAN_MASTER: "switch.fan_master",
            CONF_FAN_O1_POWER: "sensor.fan_power",
        }
    )
    hass.states.set("switch.fan_master", "off")
    hass.states.set("sensor.fan_power", "0.0")
    ctrl.applied[ZA] = PRESET_COMFORT  # heat demand via the preset fallback
    run(ctrl._reconcile_fans())  # winter run-on-loss: fans commanded on

    (evt,) = events(ctrl, "fan_change")
    assert evt["on"] is True
    assert evt["mode"] == "winter"
    assert evt["demand"] is True
    assert evt["o1_w"] == 0.0  # dial state at the moment of the change
    # The decision inputs travel with the event: no motion or running event
    # (occupied False), and no readable floor (warm None -> omitted).
    assert evt["occupied"] is False
    assert "warm" not in evt

    run(ctrl._reconcile_fans())  # steady state: no new event
    assert len(events(ctrl, "fan_change")) == 1


def test_preset_changes_carry_the_deciding_reason():
    from scout_testkit import end_booking, motion

    ctrl, hass = make_controller()
    booking(ctrl, ZA, "Beavers")
    motion(ctrl, "hall")
    run(ctrl.async_reconcile())  # comfort: booking with people present

    ctrl.seasonal_lockout = True
    run(ctrl.async_reconcile())  # ice: lockout outranks the booking

    ctrl.seasonal_lockout = False
    advance(ctrl, 20)  # motion times out mid-booking
    run(ctrl.async_reconcile())  # eco: booking gone quiet

    end_booking(ctrl, ZA)
    run(ctrl.async_reconcile())  # ice: building empty

    reasons = [(e["to"], e.get("reason")) for e in events(ctrl, "preset") if e["zone"] == ZA]
    assert reasons == [
        (PRESET_COMFORT, "booking"),
        (PRESET_ICE, "seasonal_lockout"),
        (PRESET_ECO, "booking_quiet"),
        (PRESET_ICE, "building_empty"),
    ]


def test_zone_preset_changes_are_audited():
    ctrl, hass = make_controller()
    run(ctrl._async_set_preset(ZA, PRESET_COMFORT))
    run(ctrl._async_set_preset(ZA, PRESET_COMFORT))  # unchanged: not re-recorded
    run(ctrl._async_set_preset(ZA, PRESET_ICE))
    changes = events(ctrl, "preset")
    assert [(e["zone"], e["to"]) for e in changes] == [
        (ZA, PRESET_COMFORT),
        (ZA, PRESET_ICE),
    ]
    assert changes[1]["previous"] == PRESET_COMFORT


# --- Persistence and export ---------------------------------------------------------

def test_audit_events_survive_a_restart():
    ctrl, _ = make_controller()
    ctrl.audit.record("marker", ctrl._now(), n=1)
    snap = ctrl._state_snapshot()
    assert snap["audit"][-1]["event"] == "marker"

    ctrl2, _ = make_controller()

    class _Store:
        async def async_load(self):
            return snap

    ctrl2._store = _Store()
    run(ctrl2._async_restore_state())
    assert ctrl2.audit.to_list()[-1]["event"] == "marker"


def test_trace_survives_a_restart():
    ctrl, hass = make_controller()
    _hall_temp(hass, 20)
    ctrl._sample_trace()
    snap = ctrl._state_snapshot()

    ctrl2, _ = make_controller()

    class _Store:
        async def async_load(self):
            return snap

    ctrl2._store = _Store()
    run(ctrl2._async_restore_state())
    assert ctrl2.trace.to_list() == ctrl.trace.to_list()


def test_diagnostics_data_is_json_serialisable_and_complete():
    ctrl, hass = make_controller()
    _hall_temp(hass, 20)
    hass.states.set(E["weather"], "cloudy", {"temperature": 15})
    ctrl.audit.record("marker", ctrl._now())

    data = ctrl.diagnostics_data()
    json.dumps(data)  # the diagnostics download must serialise cleanly

    assert data["tunables"]["numbers"]["preheat_minutes"]["default"] == 120.0
    assert data["tunables"]["switches"]["summer_mode"]["default"] is False
    assert data["readings"]["zones"][ZA]["coldest"] == 20.0
    assert data["readings"]["outdoor"] == 15.0
    assert data["events"][-1]["event"] == "marker"
    assert "trace" in data


def test_diagnostics_exports_raw_opening_contacts():
    # The raw door/window states must be visible, distinct from the derived
    # 10-minute `opening_ice` latch: a door physically open but not yet held
    # past the delay reads open here while `opening_ice` is still false.
    from scout_testkit import E, on

    ctrl, hass = make_controller()
    on(hass, E["a_door"])  # a hall door open, but not held long enough to ice

    openings = ctrl.diagnostics_data()["state"]["openings"]
    assert openings["zone_a_doors"][E["a_door"]] is True
    assert openings["any_open"] is True
    assert ctrl.diagnostics_data()["state"]["opening_ice"][ZA] is False


def test_diagnostics_platform_returns_the_controller_export():
    from custom_components.scout_hut_heating import diagnostics

    ctrl, hass = make_controller()
    hass.data = {DOMAIN: {ctrl.entry.entry_id: ctrl}}
    data = run(diagnostics.async_get_config_entry_diagnostics(hass, ctrl.entry))
    assert data["events"] == []
    assert "tunables" in data and "readings" in data


# --- Condensation watch and restart hardening ---------------------------------------

def test_condensation_watch_notifies_on_sustained_cold_damp():
    from custom_components.scout_hut_heating.const import CONF_CEILING_TEMP
    from scout_testkit import set_registry

    ctrl, hass = make_controller(config_overrides={CONF_CEILING_TEMP: "sensor.ceiling"})
    set_registry(
        {"dev_ht": ["sensor.ceiling", "sensor.ceiling_humidity"]},
        {"sensor.ceiling": "dev_ht"},
    )
    ctrl.seasonal_lockout = False  # heating season
    _hall_temp(hass, 10.0)  # cold fabric
    hass.states.set("sensor.ceiling_humidity", "85.0")

    ctrl._check_condensation()  # clock starts
    assert ctrl._rh_high_since is not None
    assert not events(ctrl, "condensation")

    ctrl._rh_high_since -= timedelta(hours=13)  # ...13 damp hours later
    ctrl._check_condensation()
    (evt,) = events(ctrl, "condensation")
    assert evt["rh"] == 85.0
    assert ctrl._condensation_notified is True

    hass.states.set("sensor.ceiling_humidity", "70.0")  # aired out
    ctrl._check_condensation()
    assert ctrl._condensation_notified is False
    assert ctrl._rh_high_since is None


def test_condensation_watch_is_summer_silent():
    from custom_components.scout_hut_heating.const import CONF_CEILING_TEMP
    from scout_testkit import set_registry

    ctrl, hass = make_controller(config_overrides={CONF_CEILING_TEMP: "sensor.ceiling"})
    set_registry(
        {"dev_ht": ["sensor.ceiling", "sensor.ceiling_humidity"]},
        {"sensor.ceiling": "dev_ht"},
    )
    ctrl.seasonal_lockout = True  # summer: a warm hall does not condense
    _hall_temp(hass, 10.0)
    hass.states.set("sensor.ceiling_humidity", "90.0")
    ctrl._check_condensation()
    assert ctrl._rh_high_since is None


def test_fan_timers_and_seasonal_flag_survive_a_restart():
    ctrl, _ = make_controller()
    ctrl.seasonal_lockout = True
    ctrl.fan_last_on = ctrl._now()
    ctrl.fan_last_off = ctrl._now()
    snap = ctrl._state_snapshot()

    ctrl2, _ = make_controller()

    class _Store:
        async def async_load(self):
            return snap

    ctrl2._store = _Store()
    run(ctrl2._async_restore_state())
    assert ctrl2.seasonal_lockout is True
    assert ctrl2.fan_last_on is not None
    assert ctrl2.fan_last_off is not None
