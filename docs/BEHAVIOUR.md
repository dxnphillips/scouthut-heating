# Behaviour mapping

How the original packages map onto the reconciler in `coordinator.py`. The
original YAML is preserved under [`reference/`](reference).

## Heating package (`scout_hut_heating`)

| Original | Reconciler equivalent |
| --- | --- |
| A1 / A2 Calendar pre-heat (Hall / Office) | `_async_refresh_calendars` looks ahead as far as the pre-heat cap, then computes the actual lead for the specific event found (`_async_next_event`): an ECO-keyword event aims at the eco-low target, and the room's temperature at pre-heat time is predicted by Newton cooling toward outdoor over the idle gap using the learned heat-loss constant (% of the indoor–outdoor gap lost per hour — gap-normalised so summer samples teach winter predictions; never below the 7 °C anti-frost floor; an unreadable outdoor assumes a cold 5 °C and errs warm). `_zone_preheat_minutes` = learned warm-up rate × predicted deficit + cold-weather margin, clamped to the cap; an unparseable event start errs warm. `_update_warmup_learning` times real comfort warm-ups (hall: separate fan-assisted rate, judged from O1 power) and `_update_cooloff_learning` times unheated cool-offs, normalising each by its average gap (solar gain re-anchors without learning; a sample with no readable outdoor is rejected, never guessed). All in `preheat.py`, pure and unit-tested. |
| A3 Shared-zone pre-heat | `_desired_shared` → `eco` when either calendar is in window. |
| A4 / A5 Event ended → revert | Falls out of `_desired_zone` automatically once the calendar is `off`. |
| A6 / A7 / A9 / A10 Door/Window held open → ice | `_evaluate_openings` tracks per-group "open since" and trips `opening_ice[zone]` after the door/window delay. |
| A8 / A11 All closed → restore | `opening_ice[zone]` clears when the groups close; the next reconcile restores the correct preset. |
| A11b Shared window held open → ice | `_evaluate_openings` shared branch → `opening_ice["shared"]`. |
| A12 Internal door + exterior opening → both zones ice | `through_path` in `_evaluate_openings`. |
| A13–A16 Occupied overrides | `zone_x_occupied_override` switches → `eco` in `_desired_zone`. |
| A17–A24 Motion logic (during/outside booking) | Motion timestamps (`last_motion`) + `_motion_recent*`; `_desired_zone`/`_desired_shared` encode eco-while-empty and drop-to-ice-when-empty. |
| A25 / A26 Rointe app drift detection | `_detect_drift` compares the representative heater's `preset_mode` to `expected_preset` and sets `manual_hold`. The live Rointe integration publishes `preset_mode` as null, so when it is missing the check falls back to the reported **setpoint**: each preset implies a known target (anti-frost is fixed at 7 °C; the hall comfort/eco values are the integration's own sliders; the office comfort target is the cached value from its heater). Office eco cannot be judged and is skipped rather than guessed. |
| A27 / A28 Boost | `async_boost` / `boost_until` / `boost_active`; shared zone follows via `_desired_shared`. |
| A29 Seasonal lockout | `_async_seasonal_check` (hourly + on threshold change) using `weather.get_forecasts` + RealFeel. Engages on the **3-day average** mean temperature ≥ threshold (not every high/low) and RealFeel ≥ threshold − 2. Releases at avg ≤ threshold − 0.5 or a genuine cold snap (RealFeel < threshold − 2). Engage and release are mutually exclusive (no hourly flap), and an ordinary summer night dipping a degree under the threshold does not release the lockout (which would flip the fans to the winter regime nightly). |
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
| Frost protection (new) | The Speedflow's own frost stat only works while powered, so `_desired_water` powers the tank while the coldest shared-zone room is ≤3 °C (releasing at 5 °C), overriding the alarms. |
| Weekly hygiene heat-up (new) | If the tank has gone 7 days without a completed reheat, `_desired_water` runs it for 45 min (a full 15 L reheat is ~30 min at 2 kW) so stored water never sits lukewarm indefinitely; also overrides the alarms. Only a continuous powered stretch ≥45 min resets the clock — brief dabs of use (short keep-alive, quick override) heat 15 L by only a few degrees and do not count. |

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
| Summer cooling (down air) | Regime active when `summer_mode` is on (manual force) or `summer_follows_season` (default on) + seasonal lockout engaged; runs when occupied + floor above `cooling_temp_high`. A hot-breeze guard holds the fans (and asks for doors/windows to be opened) once the estimated mixed air at head height (0.75×floor + 0.25×ceiling) reaches `cooling_mix_max_temp` (29 °C, releasing 1 °C lower). Any open mapped contact (either zone, shared, internal — all can feed a cross-draft) grants an immediate provisional pass, kept only while the venting measurably works (mix falling ≥0.3 °C per 15-min window, else the hold returns — effect-verified, not contact-modelled). The hard hold-off at `FAN_COOLING_MAX_TEMP` (35 °C — air hotter than skin heats people) remains the backstop. Direction forward. |
| Direction change | `_async_ensure_fans`: preset the O2 relay only while the master is off, otherwise press the reverse button (id 200); a `FAN_REVERSE_GRACE` window holds HA off the fans during the Shelly's 45 s sequence. |
| Sensor lost | `fans_run_on_sensor_loss` (default on): assume stratification and keep the winter fans running while demand holds; else fans off. `NOTIFY_FAN_SENSOR_LOST`. The ceiling H&T is a local threshold reporter (silence = unchanged), so its freshness is judged from entity availability only; the `last_reported` staleness window applies to the floor/Rointe readings, where a cloud value can freeze while looking alive. |
| Fault | `_fan_fault`: mapped boolean wins, else inferred from an unexpected master-off beyond `FAN_FAULT_GRACE`; refuses to run and notifies (`NOTIFY_FAN_FAULT`). Re-arm via `async_fan_rearm` (the *Ceiling fans enabled* switch off→on). |
| Dial-high reminder | `_notify_dial_high` (`NOTIFY_FAN_DIAL`) before every reversal. |

## Winter condensation watch (new)

Historic England recommends 8–10 °C background for unoccupied building
fabric; the Rointe anti-frost floor is fixed at 7 °C, so the gap is covered
by monitoring: the ceiling H&T's humidity sensor is auto-discovered, and if
the hall sits at ≥80 % RH below 12 °C for 12+ hours during the heating
season, a notification suggests background heat or airing
(`NOTIFY_CONDENSATION`, audit event `condensation`, RH recorded in the
trace). Clears when the humidity drops or the hall warms.

## Audit log & diagnostics (new)

Every decision and learning sample is appended to a bounded audit log
(`audit.py`, persisted with the state snapshot): warm-up and cool-off samples
with their raw inputs and accept/reject outcome, pre-heat window openings with
the full lead computation, the temperature-vs-target outcome at each booking
start, preset changes, fan starts/stops/reversals/faults, seasonal lockout
transitions and water frost/hygiene events. A rolling readings trace (a week
of 15-minute points) records the exact computed values the decisions used —
ceiling, the hall floor average and coldest reading, office, shared, outdoor,
fan state, heat demand and the O1 wattage (which encodes the manual
transformer dial tap; warm-up samples also carry their average O1 watts). The
whole log — plus tunables against defaults, learned rates and a live reading
snapshot — is exported by the standard Home Assistant diagnostics download
(`diagnostics.py`), so the tuning constants can be checked against the
building's real behaviour.

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
- The pre-heat lead time is adaptive (optimum start) instead of the original
  fixed 120 minutes; the slider is now the maximum. Fall-back behaviour when
  the room temperature is unreadable is the cap, i.e. exactly the original
  fixed-lead behaviour.
