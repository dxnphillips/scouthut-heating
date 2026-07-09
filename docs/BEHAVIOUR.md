# Behaviour mapping

How the original packages map onto the reconciler in `coordinator.py`. The
original YAML is preserved under [`reference/`](reference).

## Heating package (`scout_hut_heating`)

| Original | Reconciler equivalent |
| --- | --- |
| A1 / A2 Calendar pre-heat (Hall / Office) | `_async_refresh_calendars` caches an in-window flag + event title per zone; `_desired_zone` treats the pre-heat window like an active booking. |
| A3 Shared-zone pre-heat | `_desired_shared` → `eco` when either calendar is in window. |
| A4 / A5 Event ended → revert | Falls out of `_desired_zone` automatically once the calendar is `off`. |
| A6 / A7 / A9 / A10 Door/Window held open → ice | `_evaluate_openings` tracks per-group "open since" and trips `opening_ice[zone]` after the door/window delay. |
| A8 / A11 All closed → restore | `opening_ice[zone]` clears when the groups close; the next reconcile restores the correct preset. |
| A11b Shared window held open → ice | `_evaluate_openings` shared branch → `opening_ice["shared"]`. |
| A12 Internal door + exterior opening → both zones ice | `through_path` in `_evaluate_openings`. |
| A13–A16 Occupied overrides | `zone_x_occupied_override` switches → `eco` in `_desired_zone`. |
| A17–A24 Motion logic (during/outside booking) | Motion timestamps (`last_motion`) + `_motion_recent*`; `_desired_zone`/`_desired_shared` encode eco-while-empty and drop-to-ice-when-empty. |
| A25 / A26 Rointe app drift detection | `_detect_drift` compares the representative heater's `preset_mode` to `expected_preset` and sets `manual_hold`. |
| A27 / A28 Boost | `async_boost` / `boost_until` / `boost_active`; shared zone follows via `_desired_shared`. |
| A29 Seasonal lockout | `_async_seasonal_check` (hourly + on threshold change) using `weather.get_forecasts` + RealFeel. Engages on the **3-day average** mean temperature ≥ threshold (not every high/low), so a warm season locks out even with cool nights. |
| A30 Automation re-enabled | Toggling the enable switch requests a reconcile. |
| A31 Nightly safety net | Implicit: with no booking/motion the reconciler already targets `ice`. |
| A32 Startup initialise | `async_start` schedules a first reconcile after a 30 s delay. |
| A33 / A34 Alarm set → cancel manual/motion heat | `_desired_zone` returns `ice` when the zone alarm is on with no booking. |
| A35 Alarm cleared → re-evaluate | Alarm state changes are watched and trigger a reconcile. |

## Water heater package (`scout_hut_water_heater`)

| Original | Reconciler equivalent |
| --- | --- |
| W1 Calendar pre-heat | `water_window` (own pre-heat lead time) in `_desired_water`. |
| W2 Motion → on | Kitchen/gents/female motion within the keep-alive in `_desired_water`. |
| W3 No motion → off | Same, evaluated every tick. |
| W4 / W5 Manual override on/off | `water_manual_override` switch. |
| W6 Nightly off | Implicit: no calendar/motion/override → off. |
| W7 Startup init | Covered by the startup reconcile. |
| W8 / W9 Alarm set/cleared | `both_alarms` branch in `_desired_water`; alarm changes trigger a reconcile. |

## Ceiling fans (new — no original automation)

The destratification / cooling fans have no equivalent in the original packages;
they are added on the same reconciler. The pure decision lives in
[`fan_logic.py`](../custom_components/scout_hut_heating/fan_logic.py) (unit
tested offline in [`tests/test_fan_decision.py`](../tests/test_fan_decision.py));
the coordinator's `_reconcile_fans` / `_async_ensure_fans` gather the live
signals and drive the Shelly.

| Behaviour | Reconciler |
| --- | --- |
| Winter destratification (up air) | `_fan_target` + `fan_decision`: ceiling-floor ΔT above `fan_dt_on` **and** the heat is worth moving — `_heat_demand()` true (any Rointe *Effective Power* over `heat_demand_watts`, across hall/office/shared) **or** the floor is below `fan_recirc_max_floor_temp`. The recirculation term decouples the fans from the heater's on/off cycle, so residual heat is harvested after a heater cuts out. Runs for loss reduction as well as comfort, so it is **not** gated on hall occupancy. Hysteresis via `fan_dt_off`, `fan_min_run_minutes`, `fan_min_off_minutes`. Direction reverse. |
| Summer cooling (down air) | `summer_mode` on + occupied + floor above `cooling_temp_high`. Direction forward. |
| Direction change | `_async_ensure_fans`: preset the O2 relay only while the master is off, otherwise press the reverse button (id 200); a `FAN_REVERSE_GRACE` window holds HA off the fans during the Shelly's 45 s sequence. |
| Sensor lost | `fans_run_on_sensor_loss` (default on): assume stratification and keep the winter fans running while demand holds; else fans off. `NOTIFY_FAN_SENSOR_LOST`. |
| Fault | `_fan_fault`: mapped boolean wins, else inferred from an unexpected master-off beyond `FAN_FAULT_GRACE`; refuses to run and notifies (`NOTIFY_FAN_FAULT`). Re-arm via `async_fan_rearm` (the *Ceiling fans enabled* switch off→on). |
| Dial-high reminder | `_notify_dial_high` (`NOTIFY_FAN_DIAL`) before every reversal. |

## Rointe offline / stale handling

The Rointe integration is cloud based, so a heater can go offline or stop
updating. The reconciler is defensive about this:

- **Fan floor temperature & heat-demand** ignore a heater that is unavailable or
  has stopped reporting (judged from `last_reported`), so a frozen reading is
  never trusted; if nothing readable remains the floor is treated as lost.
- **Re-send after reconnect** — `_async_set_preset` records when a preset was
  sent while a heater was offline (`_zone_offline_apply`); `_reconcile_zones`
  re-sends once every heater in the zone is back online (`_all_zone_online`), so
  a heater cannot get stuck on the wrong preset after a blip.
- **Drift detection pauses while offline** — `_detect_drift` skips a zone whose
  representative heater is not reachable, so a stale preset is not mistaken for a
  manual change.

Reachability comes from each heater's Rointe **Connected** binary_sensor
(auto-detected from the device, like the Effective Power sensors), falling back
to the climate entity's own availability when no connectivity sensor exists.

## Deliberate differences

- A single 30-second reconcile replaces the mix of 1- and 5-minute polls, so
  responses are at least as quick.
- Presets are only re-sent when the target actually changes, cutting chatter to
  the heaters.
- Motion is tracked with in-memory "last seen" timestamps (equivalent to the
  original `input_datetime` helpers) so pulse sensors work correctly.
- A zone with no booking, override or motion of its own rests at `eco` while
  people remain elsewhere in the building (the original left the last preset —
  possibly `comfort` — running), dropping to `ice` only when the building
  empties.
