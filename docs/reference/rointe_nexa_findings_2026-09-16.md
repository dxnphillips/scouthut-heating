# Rointe integration findings (16 September 2026)

Owner-supplied. Source: code review of `JYewman/rointe_integration` v3.0.4 (latest HACS
release), plus four live read-only audits of the Rointe Nexa cloud account between 13:22 and
13:28 BST with a baseline an hour earlier. The config entry uses the **Nexa** backend. There
are 8 heaters in two Rointe cloud zones: **"Hall and Office"** (Hall Front, Hall Right, Hall
Back, Hall Left, Office) and **"Kitchen and Toilets"** (Kitchen, Ladies Toilet, Gents Toilet).

Labels: **CONFIRMED** = seen in live cloud data. **CODE** = read from the integration source.
**UNVERIFIED** = likely but not yet proven.

See `CLAUDE.md` (the Rointe stale-cache bullet and open question 25) for how these findings
map onto this integration's code and the agreed fix sequence.

## How the integration behaves on Nexa

- CODE: Polls every 15 s. Each poll makes a REST installation call, a REST statistics call,
  and opens new WebSocket connections per heater (data plus firmware), roughly 17
  connections per poll.
- CODE: Every climate command (`set_temperature`, `set_hvac_mode`, `set_preset_mode`)
  triggers an immediate, undebounced full refresh of all 8 heaters. A four-heater change
  means four full polls.
- CODE: A preset on Nexa is not a mode. `set_preset_mode` copies the integration's **cached**
  comfort, eco or ice value into the live setpoint and writes status `"none"`.
- CODE: Writing a comfort or eco number entity updates the cloud but **not** the
  integration's cache, and only requests a debounced refresh (about a 10 s cooldown). A
  preset applied soon after a number write therefore sends the **old** value.
- CODE: Write acknowledgements are mostly ignored. No exception does not prove the write
  landed.
- CODE: A failed read or write marks that heater unavailable until the next good poll, which
  causes short unavailable blips.
- CODE: One malformed field in any heater's payload fails the whole poll, and every heater
  goes unavailable.
- CODE: Entity state is rewritten every poll, so `last_reported` and `last_updated` are
  always fresh even when the underlying values are frozen.
- CODE + CONFIRMED: The Nexa REST token lasts 168 hours (7 days). The integration never
  renews it, even though a refresh token is issued. After 7 days without a restart or reload,
  all heaters go unavailable until the Rointe integration is reloaded. This is the most likely
  cause of the 27-hour outage on 29 July.
- CODE: Switch entities, boost and schedule services, and the min and max temperature numbers
  silently do nothing on Nexa.

## Confirmed in live data

- Commands reach the heaters in 2 to 5 seconds. The Rointe cloud path is fast, so setpoint
  lag is caused inside the integration.
- Setpoint lag bug reproduced: after the 13:22 motion event the Office had comfort 21.5 but a
  live setpoint of 21.0 (the previous comfort value).
- Heaters set their own status to the matching preset (eco or comfort) shortly after a write.
  The next HA write resets it to `"none"`, so `preset_mode` flickers between a preset and
  null.
- Hall heaters received two writes about 3 to 7 seconds apart during one motion event.
- `status_warming` is 2 on every heater in every run, including at the 7 °C frost setpoint
  with rooms at 18 to 20 °C. It carries no heating information.
- Cloud values only change when a heater syncs. Daytime sync gaps of 1 to 16 minutes were
  seen, and idle heaters may only sync on change. Overnight gaps are not yet measured.
- `temp_probe` is present on all 8 heaters, in 0.5 °C steps.
- `temp_surface` is live and moves fast: Ladies Toilet dropped from 47.5 to 27.5 °C in six
  minutes after syncing.
- `active_power` exists only on Hall Left and Ladies Toilet. It held steady over nine minutes
  and changed over an hour. UNVERIFIED: it is probably an hourly average rather than live
  draw.
- Energy statistics: an installation total, with zones carrying cost only. Per-heater energy
  is an estimate split by zone cost share, then divided equally across the zone's heaters
  regardless of their power rating (600, 1200 and 1800 W models exist). Office energy cannot
  be separated from the hall. Rointe's `savingPercentage` reads 143 %, so its statistics are
  not trustworthy.
- One heater has WiFi signal down to −78 dBm (marginal). Which heater is not yet identified.
- `block_remote` is true on all heaters, yet cloud control works. Its meaning is unknown.
- `ice_mode` is true on all heaters.

## What can and cannot be relied on

| Signal | Rely on it? | Notes |
|:--|:--|:--|
| `current_temperature` (`temp_probe`) | Yes, with care | 0.5 °C steps. Only as fresh as the last device sync. Can freeze while the entity looks healthy. |
| `target_temperature` (`temp`) | As cloud truth only | May be a stale copy of the previous comfort or eco value. Compare with the value we intended, not with the number entity. |
| `preset_mode` | No | Flickers between the preset name and null. |
| `hvac_action` | No | Derived from probe against a possibly stale setpoint. |
| `heating_status` sensor, heating binary sensor | No | Based on `status_warming`, which is always 2. |
| Rointe surface temperature sensor | Best available heating evidence | Subject to sync freshness. A rising surface means real heating. |
| Rointe energy sensor | Zone estimate only | Identical across heaters in a Rointe zone, so never sum it across heaters. Can drop, which HA treats as a meter reset. |
| Availability | Partly | Single-poll blips are noise. Sustained unavailability of all heaters most likely means token expiry. |
| `last_reported`, `last_updated` | No, for freshness | Refreshed every 15 s regardless of the data. |
| Comfort and eco number entities | Reflect cloud values | Changing them does not move the live setpoint. |
| Rointe switches, boost, schedule, min and max numbers | No | Silent no-ops on Nexa. |
| A service call completing without error | No | Not proof the write was applied. |

## Recommended changes in Scout Hut Heating (owner's list)

1. Stop relying on `set_preset_mode` to apply values. After writing the comfort or eco number
   with `blocking=True`, apply the live setpoint with `climate.set_temperature` using the
   intended value. On Nexa this writes the same fields as a preset but uses our value instead
   of the stale cache. Keep writing the numbers so the heater's own buttons match. The climate
   accepts 7 to 40 °C and rounds to 0.5.
2. Use `blocking=True` for heater calls, catch `HomeAssistantError`, and record write
   failures in the audit trail. Currently applied state and offline checks run before the
   write has happened.
3. Judge drift by comparing the live setpoint with the value we intended. Never use
   `preset_mode`.
4. Reduce writes: skip unchanged values, avoid reasserting identical setpoints, and space
   heater writes a second or two apart, because each command triggers a full poll of all 8
   heaters.
5. Treat `heating_status` and `hvac_action` as unusable. Use surface temperature rise as
   corroborating evidence of real heating.
6. The staleness guard based on `last_reported` cannot detect frozen probes. Options:
   independent room sensors; the upstream integration exposing `last_sync_datetime_device`
   as a timestamp sensor; or flat-reading detection with long thresholds (normal sync gaps
   reach at least 16 minutes).
7. Build the sustained heater outage alert (Q18). Consider an interim automation that reloads
   the Rointe config entry every six days at a quiet time until the token bug is fixed
   upstream. That reload approach is not yet decided.
8. Energy: read one representative heater per Rointe zone, treat drops as resets, and
   remember the office shares a Rointe zone with the hall.
9. Diagnostics: redact booking titles (`cal_title`), which can contain hirer names.

## Do not do

- Do not write `block_remote`, `block_local`, `ice_mode`, `power`, `check_updates_now`,
  `debug_mode` or `debug_server_path`. Some could lock out remote control, remove frost
  protection, or send data elsewhere.
- `um_password` (the heaters' local lock PIN) is readable by anyone holding the Rointe login.
  Do not reuse that PIN anywhere else.
- Do not patch the Rointe integration in place, because HACS updates overwrite it. Use
  upstream pull requests or a fork that HACS installs from.

## Upstream issues to raise on the Rointe integration

1. TLS certificate verification is disabled (`CERT_NONE`) on Nexa WebSockets. A verified
   connection was tested and works, so the fix is safe.
2. The Nexa REST token is never renewed, failures are silent, and there is no
   reauthentication flow.
3. Presets copy stale cached values, and number writes do not update the cache.
4. Polling and command load: a new connection per read, and an undebounced full refresh
   after every command.
5. Write acknowledgements are not checked.
6. Many controls are silently dropped on Nexa.
7. `coordinator.data` empties after the second poll, which breaks the services.
8. One malformed field fails the whole poll.
9. Feature request: expose `last_sync_datetime_device`, `temp_surface` freshness and
   `active_power`.

## Open questions

- Heaters showed surfaces of 33 to 47.5 °C at 13:22:05 BST, before the motion event could
  have heated them, and installation energy rose 1.84 kWh between 12:22 and 13:19 BST, while
  every audit snapshot showed 7 °C setpoints. HA wrote to every heater at 13:03 BST without
  changing the setpoint. Check HA history and the Scout Hut Heating audit trail for 12:00 to
  13:25 BST to see whether setpoints were raised between snapshots. (From this integration's
  side, a write to every heater that does not change the setpoint is a preset **re-apply** —
  a restart/reload re-applies all presets, and `_zone_offline_apply` re-sends after a heater
  blips unavailable.)
- Is `active_power` an hourly average? A run just after an hour boundary would tell.
- What are the normal overnight sync gaps for an idle heater?
- Which heater has the weak WiFi signal?
- What does `block_remote` actually do on these models?
