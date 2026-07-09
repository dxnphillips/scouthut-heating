"""Offline decision-table tests for the fan logic.

These exercise the pure ``fan_decision`` function with no Home Assistant
dependency, so they run anywhere with just ``python -m pytest`` (or the plain
``python tests/test_fan_decision.py`` self-check at the bottom).

They lock in the behaviour that matters:
  * winter hysteresis (start above dt_on, keep running until dt_off),
  * all three winter stop conditions,
  * the "run when the sensor is lost" preference (assume stratification),
  * summer forward-cooling only when present and warm,
  * mode/direction mapping (reverse = winter up air, forward = summer down air).
"""

from __future__ import annotations

import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "custom_components", "scout_hut_heating")
)

from fan_logic import fan_decision  # noqa: E402

DT_ON = 3.0
DT_OFF = 1.0


def winter(**kw):
    base = dict(
        summer=False,
        occupied=True,
        warm=None,
        dt=5.0,
        dt_on=DT_ON,
        dt_off=DT_OFF,
        demand=True,
        currently_winter=False,
        run_on_loss=True,
    )
    base.update(kw)
    return fan_decision(**base)


def summer(**kw):
    base = dict(
        summer=True,
        occupied=True,
        warm=True,
        dt=None,
        dt_on=DT_ON,
        dt_off=DT_OFF,
        demand=False,
        currently_winter=False,
        run_on_loss=True,
    )
    base.update(kw)
    return fan_decision(**base)


# --- Winter start / hysteresis -------------------------------------------------

def test_winter_starts_above_dt_on():
    assert winter(dt=5.0) == (True, "reverse", "winter")


def test_winter_does_not_start_at_dt_on():
    # Strictly greater than dt_on is required to start.
    assert winter(dt=3.0) == (False, None, "off")


def test_winter_does_not_start_between_thresholds():
    # 2 degrees: above dt_off but not above dt_on -> stays off until it climbs.
    assert winter(dt=2.0) == (False, None, "off")


def test_winter_keeps_running_between_thresholds():
    # Once running, it holds through the hysteresis band down to dt_off.
    assert winter(dt=2.0, currently_winter=True) == (True, "reverse", "winter")


def test_winter_stops_at_dt_off():
    assert winter(dt=1.0, currently_winter=True) == (False, None, "off")


# --- Winter stop conditions ----------------------------------------------------

def test_winter_stops_when_heat_stops():
    assert winter(dt=5.0, currently_winter=True, demand=False) == (False, None, "off")


def test_winter_stops_when_unoccupied():
    assert winter(dt=5.0, currently_winter=True, occupied=False) == (False, None, "off")


def test_winter_needs_demand_to_start():
    assert winter(dt=5.0, demand=False) == (False, None, "off")


def test_winter_needs_occupancy_to_start():
    assert winter(dt=5.0, occupied=False) == (False, None, "off")


# --- Sensor loss ---------------------------------------------------------------

def test_sensor_loss_runs_when_preferred():
    # dt is None (sensor lost) but heat + occupancy present, run_on_loss on.
    assert winter(dt=None, run_on_loss=True) == (True, "reverse", "winter")


def test_sensor_loss_off_when_not_preferred():
    assert winter(dt=None, run_on_loss=False) == (False, None, "off")


def test_sensor_loss_still_needs_demand_and_occupancy():
    assert winter(dt=None, run_on_loss=True, demand=False) == (False, None, "off")
    assert winter(dt=None, run_on_loss=True, occupied=False) == (False, None, "off")


# --- Summer cooling ------------------------------------------------------------

def test_summer_cools_when_present_and_warm():
    assert summer(warm=True, occupied=True) == (True, "forward", "summer")


def test_summer_off_when_empty():
    assert summer(warm=True, occupied=False) == (False, None, "off")


def test_summer_off_when_not_warm():
    assert summer(warm=False, occupied=True) == (False, None, "off")


def test_summer_off_when_floor_unknown():
    # No floor reading -> cannot confirm warmth -> do not blow air.
    assert summer(warm=None, occupied=True) == (False, None, "off")


def test_summer_ignores_winter_heat_demand():
    # Summer regime never reverses, even if a radiator happens to draw power.
    assert summer(warm=True, occupied=True, demand=True) == (True, "forward", "summer")


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)
