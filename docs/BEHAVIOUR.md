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
| A29 Seasonal lockout | `_async_seasonal_check` (daily 08:00 + on threshold change) using `weather.get_forecasts` + RealFeel. |
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

## Deliberate differences

- A single 30-second reconcile replaces the mix of 1- and 5-minute polls, so
  responses are at least as quick.
- Presets are only re-sent when the target actually changes, cutting chatter to
  the heaters.
- Motion is tracked with in-memory "last seen" timestamps (equivalent to the
  original `input_datetime` helpers) so pulse sensors work correctly.
