"""Regression tests for the 2026-07-18 adversarial code-review fixes."""

import custom_components.scout_hut_heating.coordinator as C
from scout_testkit import (
    PRESET_ECO,
    ZA,
    FakeNum,
    booking,
    make_controller,
    motion,
    run,
    service_calls,
    set_registry,
)


# --- Fix 5: cool-off min-gap floor raised 3.0 -> 4.0 -------------------------
def test_cooloff_rejects_small_gap_below_the_raised_floor():
    from custom_components.scout_hut_heating.preheat import updated_cooling_k

    # gap 3.5 passed the old 3.0 floor and spiked the learned rate to ~20 %/h;
    # now rejected (returns k unchanged).
    assert updated_cooling_k(0.13, 0.8, 1.0, 3.5) == 0.13
    assert updated_cooling_k(0.13, 1.5, 1.0, 3.99) == 0.13
    # gap at/above the new 4.0 floor still learns.
    assert updated_cooling_k(0.13, 1.5, 1.0, 6.0) != 0.13


# --- Fix 6: single-tick step guard (office EWMA corruption, 2026-07-22) -------
def test_cooloff_single_tick_step_is_rejected():
    from custom_components.scout_hut_heating.preheat import updated_cooling_k

    # A discontinuity — an open office window (no contact sensor to flag it) or
    # a frozen Rointe probe catching up — dumps the whole drop into one tick.
    # Rejected even with a genuine gap and duration (returns k unchanged).
    assert updated_cooling_k(0.05, 2.0, 1.5, 6.0, 1.5) == 0.05
    # The same total drop spread across ticks (max single step 0.5 °C) learns.
    assert updated_cooling_k(0.05, 2.0, 1.5, 6.0, 0.5) != 0.05


# --- Fix 3: pre-heat rate_key reports the rate actually used -----------------
def test_prediction_rate_key_matches_the_value_used():
    ctrl, _ = make_controller(
        config_overrides={C.CONF_FAN_MASTER: "switch.fan_master"}
    )
    # Fan-assisted rate still at its MAX seed (60); base learned lower (45).
    ctrl.register_number("zone_a_warmup_rate_fans", FakeNum(60.0))
    ctrl.register_number("zone_a_warmup_rate", FakeNum(45.0))
    rate, key = ctrl._prediction_rate(ZA)
    assert rate == 45.0
    assert key == "zone_a_warmup_rate"  # not the preferred "...fans"


# --- Fix 1: cal_window survives a restart so the pause is not phantom-cleared -
def test_cal_window_persisted_and_restored():
    ctrl, _ = make_controller()
    ctrl.hall_heating_paused = True
    ctrl.cal_window[ZA] = True  # a booking is running while paused
    snap = ctrl._state_snapshot()
    assert snap["cal_window"][ZA] is True

    # A fresh controller resets cal_window to False on construction; restoring
    # must bring it back so _async_refresh_calendars sees True->True and does
    # NOT read a phantom idle->session edge that would clear the pause.
    ctrl2, _ = make_controller()

    async def _load():
        return snap

    ctrl2._store.async_load = _load
    run(ctrl2._async_restore_state())
    assert ctrl2.hall_heating_paused is True
    assert ctrl2.cal_window[ZA] is True


# --- Fix 2: eco-low re-pushed when it changes without a preset transition ----
def _hall_eco_registry():
    set_registry(
        entries_by_device={
            "d1": [
                "climate.hall_back",
                "number.hall_back_comfort_temperature",
                "number.hall_back_eco_temperature",
            ],
            "d2": [
                "climate.hall_front",
                "number.hall_front_comfort_temperature",
                "number.hall_front_eco_temperature",
            ],
        },
        entity_devices={"climate.hall_back": "d1", "climate.hall_front": "d2"},
    )


def _last_eco_value(hass):
    for c in reversed(service_calls(hass, "number", "set_value")):
        ids = c["data"].get("entity_id")
        ids = ids if isinstance(ids, list) else [ids]
        if any("eco" in (e or "") for e in ids):
            return c["data"]["value"]
    return None


def test_eco_low_repushed_when_keyword_starts_on_an_already_eco_hall():
    _hall_eco_registry()
    ctrl, hass = make_controller()
    # Hall goes to eco via motion first (no keyword) -> device eco = 16.
    motion(ctrl, "hall")
    run(ctrl.async_reconcile())
    assert ctrl.applied[ZA] == PRESET_ECO
    assert _last_eco_value(hass) == ctrl.number("hall_eco_temp")  # 16

    # A keyword ("test") booking now starts while the hall is ALREADY eco, so
    # the preset does not change and the per-transition push never fires.
    booking(ctrl, ZA, title="test")
    run(ctrl.async_reconcile())
    # The reconcile backstop re-pushed the eco-LOW value (14), not the 16.
    assert _last_eco_value(hass) == ctrl.number("hall_eco_low_temp")  # 14


# --- Fix 4: a swallowed setpoint push is audited, not silent -----------------
def test_hall_temp_push_skipped_is_audited_when_numbers_missing():
    set_registry(entries_by_device={}, entity_devices={})  # nothing discoverable
    ctrl, _ = make_controller()
    motion(ctrl, "hall")  # -> eco, so a push is attempted with no target
    run(ctrl.async_reconcile())
    skipped = [e for e in ctrl.audit.to_list() if e.get("event") == "hall_temp_push_skipped"]
    assert skipped and skipped[-1]["eco_found"] is False
