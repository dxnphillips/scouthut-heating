"""Control logic for the Scout Hut Heating integration.

This module replaces the ~35 automations, scripts and template sensors of the
original YAML packages with a single reconciler. On every tick (and whenever a
relevant sensor, calendar, alarm or helper changes) it recomputes the preset
each zone *should* be running from the same priority rules as the original
package, and only calls a service when the target actually changes.

Priority (highest wins), per heated zone:
    1. Automation disabled / manual hold  -> leave the heater alone
    2. Opening held open (door/window)    -> ice
    3. Boost active                       -> comfort (bypasses seasonal lockout)
    4. Seasonal lockout                   -> ice
    5. Alarm set with no booking          -> ice (clears occupied override)
    6. Calendar booking / pre-heat window -> comfort (eco for ECO-keyword events);
                                             drops to eco while unoccupied
    7. Occupied override / recent motion  -> eco
    8. Zone empty                          -> eco while someone is elsewhere in
                                             the building, ice once it is empty
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .audit import AuditLog, Trace
from .fan_logic import fan_decision
from .preheat import (
    MAX_RATE,
    MIN_COOL_SAMPLE_DROP,
    MIN_COOL_SAMPLE_GAP,
    MIN_COOL_SAMPLE_HOURS,
    MIN_SAMPLE_MINUTES,
    MIN_SAMPLE_RISE,
    required_lead_minutes,
    updated_cooling_k,
    updated_rate,
)
from .const import (
    CONF_ALARM_MAIN,
    CONF_ALARM_OFFICE,
    CONF_CALENDAR_HALL,
    CONF_CALENDAR_OFFICE,
    CONF_CEILING_TEMP,
    CONF_FAN_DIRECTION,
    CONF_FAN_FAULT,
    CONF_FAN_MASTER,
    CONF_FAN_O1_POWER,
    CONF_FAN_REVERSE,
    CONF_FLOOR_TEMP,
    CONF_HALL_CLIMATES,
    CONF_HALL_COMFORT_NUMBERS,
    CONF_HALL_ECO_NUMBERS,
    CONF_INTERNAL_DOOR,
    CONF_MOTION_FEMALE,
    CONF_MOTION_GENTS,
    CONF_MOTION_HALL,
    CONF_MOTION_KITCHEN,
    CONF_MOTION_OFFICE,
    CONF_OFFICE_CLIMATES,
    CONF_REALFEEL,
    CONF_ROINTE_POWER,
    CONF_SHARED_CLIMATES,
    CONF_SHARED_WINDOWS,
    CONF_WATER_SWITCH,
    CONF_WEATHER,
    CONF_ZONE_A_DOORS,
    CONF_ZONE_A_WINDOWS,
    CONF_ZONE_B_DOORS,
    CONF_ZONE_B_WINDOWS,
    DOMAIN,
    FAN_COOLING_MAX_TEMP,
    NOTIFY_CONDENSATION,
    NOTIFY_FAN_BREEZE,
    NOTIFY_FAN_DIAL,
    NOTIFY_FAN_FAULT,
    NOTIFY_FAN_SENSOR_LOST,
    NOTIFY_FAN_TOO_HOT,
    NOTIFY_INTERNAL_DOOR,
    NOTIFY_SEASONAL,
    NOTIFY_SHARED_OPENING,
    NOTIFY_ZONE_HOLD,
    NOTIFY_ZONE_OPENING,
    NUMBER_DEFS,
    PRESET_COMFORT,
    PRESET_ECO,
    PRESET_ICE,
    RECONCILE_INTERVAL,
    STARTUP_DELAY,
    SWITCH_DEFS,
    WATER_FROST_OFF_TEMP,
    WATER_FROST_ON_TEMP,
    WATER_HYGIENE_INTERVAL,
    WATER_HYGIENE_MINUTES,
    ZONE_A,
    ZONE_B,
)

# The Shelly reverse sequence takes ~45 s of coast-down plus a settle. While it
# runs, the master relay legitimately reads off. Home Assistant must not touch
# the fans during this window, and must not mistake the dwell for a fault.
FAN_REVERSE_GRACE = 70  # seconds
FAN_FAULT_GRACE = 70  # seconds a master may be unexpectedly off before we latch
# Pause between presetting the direction relay and closing the master, so the
# Finder contactor has finished travelling before the load is applied (the
# Shelly script uses the same settle inside its own reversal sequence).
FAN_DIRECTION_SETTLE = 1.5  # seconds

# Hysteresis on the seasonal lockout: engage at avg >= threshold, release only
# once the 3-day average drops this far below it (or on a cold-snap RealFeel),
# so a forecast hovering at the threshold cannot flap the lockout hourly.
SEASONAL_RELEASE_BAND = 0.5  # °C
# A RealFeel "cold snap" only releases the lockout when it is this far BELOW
# the threshold. Ordinary summer nights dip a degree or two under the
# threshold; without this band every mild night released the lockout (and
# flipped the fans to the winter regime) until the next warm morning.
SEASONAL_SNAP_BAND = 2.0  # °C

# O1 power above this means the fans are genuinely moving air (a closed master
# with the transformer dial at zero draws next to nothing). Just below the
# Shelly script's MIN_RUN_W commissioning placeholder.
FAN_RUNNING_MIN_WATTS = 20.0

# Hot-breeze guard ventilation override: an open door/window grants the fans
# a provisional pass (a cross-breeze helps even in warm air), kept while the
# venting is at least HOLDING the line. The test is trend-direction, not
# speed: measured no-venting solar charge RAISES the mix ~1.8 °C/h, while
# genuine venting against a small indoor-outdoor gap may only manage a slow
# drift down — so the pass is revoked only when the mix climbs this far above
# the best (lowest) value seen since venting began. Flat or falling = the
# venting is making a difference. "It's not about what is open, it's about
# what is actually making a difference."
BREEZE_VENT_MAX_RISE = 0.5  # °C above the best mix seen while venting

# Winter condensation watch (Historic England: unoccupied fabric is happiest
# at 8-10 °C; the Rointe anti-frost floor is fixed at 7, so the gap is covered
# by monitoring). Sustained high humidity in a cold hall is the condensation /
# mould signature; the fix (background heat, ventilation) is a human decision.
CONDENSATION_RH_ON = 80.0  # % — start the clock at/above this
CONDENSATION_RH_OFF = 75.0  # % — clock keeps running until RH drops below this
CONDENSATION_MAX_TEMP = 12.0  # °C — only a COLD hall condenses on its fabric
CONDENSATION_HOURS = 12.0  # sustained hours before notifying

# Consecutive reverse-button presses that fail to change the direction relay
# before the controller concludes the Shelly script is absent/broken and
# latches a fault instead of pressing forever.
MAX_REVERSE_ATTEMPTS = 3

# Ignore preset drift within this window of our own change: the Rointe cloud
# can take a couple of minutes to reflect a preset we just sent, and a shorter
# settle produced phantom "manual control detected" holds.
DRIFT_SETTLE_SECONDS = 180

# The Rointe integration in the field accepts set_preset_mode but publishes
# preset_mode as null, so drift falls back to the reported setpoint: Rointe
# presets pin the target temperature, and anti-frost is fixed on the hardware.
# The tolerance sits under the 0.5° UI step, so the smallest possible manual
# adjustment is still detected while float noise is not.
SETPOINT_TOLERANCE = 0.3  # °C
ROINTE_ANTIFROST = 7.0  # °C

_LOGGER = logging.getLogger(__name__)

SIGNAL_UPDATE = f"{DOMAIN}_update"

# Per-zone entity-map keys.
ZONE_CLIMATES = {ZONE_A: CONF_HALL_CLIMATES, ZONE_B: CONF_OFFICE_CLIMATES}
ZONE_CALENDAR = {ZONE_A: CONF_CALENDAR_HALL, ZONE_B: CONF_CALENDAR_OFFICE}
ZONE_ALARM = {ZONE_A: CONF_ALARM_MAIN, ZONE_B: CONF_ALARM_OFFICE}
ZONE_DOORS = {ZONE_A: CONF_ZONE_A_DOORS, ZONE_B: CONF_ZONE_B_DOORS}
ZONE_WINDOWS = {ZONE_A: CONF_ZONE_A_WINDOWS, ZONE_B: CONF_ZONE_B_WINDOWS}
ZONE_MOTION_AREA = {ZONE_A: "hall", ZONE_B: "office"}

MOTION_AREAS = {
    "hall": CONF_MOTION_HALL,
    "office": CONF_MOTION_OFFICE,
    "kitchen": CONF_MOTION_KITCHEN,
    "gents": CONF_MOTION_GENTS,
    "female": CONF_MOTION_FEMALE,
}
WATER_MOTION_AREAS = ("kitchen", "gents", "female")


class ScoutController:
    """Reconciles heating and hot water against calendar, motion and weather."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialise the controller."""
        self.hass = hass
        self.entry = entry
        self.config: dict[str, Any] = {**entry.data, **entry.options}

        # Registries populated by the tunable input platforms.
        self._numbers: dict[str, Any] = {}
        self._switches: dict[str, Any] = {}
        self._selects: dict[str, Any] = {}
        self._texts: dict[str, Any] = {}

        # Internal state.
        self.last_motion: dict[str, datetime | None] = {a: None for a in MOTION_AREAS}
        self.open_since: dict[str, datetime | None] = {}
        self.opening_ice: dict[str, bool] = {ZONE_A: False, ZONE_B: False, "shared": False}
        self.manual_hold: dict[str, bool] = {ZONE_A: False, ZONE_B: False}
        self.boost_until: dict[str, datetime | None] = {ZONE_A: None, ZONE_B: None}
        self.seasonal_lockout = False
        self.expected_preset: dict[str, str | None] = {ZONE_A: None, ZONE_B: None}
        self.applied: dict[str, str | None] = {ZONE_A: None, ZONE_B: None, "shared": None}
        self.water_on: bool | None = None
        self.water_frost_active = False  # shared zone near freezing: keep powered
        self.water_on_since: datetime | None = None  # start of current powered stretch
        self.water_last_hot: datetime | None = None  # last COMPLETED full reheat
        self.water_hygiene_until: datetime | None = None  # weekly heat-up window
        self._last_apply: dict[str, datetime] = {}

        # Fan / destratification state.
        self.fan_on: bool | None = None            # last commanded on/off
        self.fan_mode: str = "off"                 # "winter" | "summer" | "off"
        self.fan_direction: str | None = None      # "reverse" | "forward"
        self.fan_last_on: datetime | None = None
        self.fan_last_off: datetime | None = None
        self.fan_dt: float | None = None           # ceiling - floor (diagnostic)
        self.fan_overheated: bool = False          # room past the fan-cooling ceiling
        self.fan_breeze_hot: bool = False          # breeze guard holding (mix hot, hall shut)
        self._breeze_latch = False                 # raw mixed-air-too-warm latch
        self._vent_anchor_mix: float | None = None  # best (lowest) mix while venting
        self._vent_effective = True
        self.fan_mix: float | None = None          # estimated mixed-air temp at head height
        self.heat_demand: bool = False             # any radiator drawing power
        self.fan_sensor_stale: bool = False        # ceiling/floor lost
        self.fan_fault_latched: bool = False       # inferred (unpublished) fault
        self.fan_master_expected: bool | None = None  # what we last told O1 to be
        self.fan_master_off_since: datetime | None = None  # for dwell-safe infer
        self.fan_action_grace_until: datetime | None = None  # Shelly mid-sequence
        # The occupancy/warmth inputs behind the last fan decision, stashed so
        # fan_change audit events can carry them (a stopped fan is otherwise
        # ambiguous between "nobody there" and "not warm enough").
        self._fan_occupied: bool | None = None
        self._fan_warm: bool | None = None
        # Winter condensation watch state.
        self._humidity_entity: str | None = None  # auto-found ceiling RH sensor
        self._rh_high_since: datetime | None = None
        self._condensation_notified = False
        self._fan_master_seen_unavailable = False  # device rebooted, not manual off
        self._reverse_attempts = 0  # consecutive reversals with no relay change
        self._fan_fault_notified: bool = False
        self._discovered_power: list[str] | None = None  # auto-found power sensors
        self._connected_map: dict[str, str] | None = None  # climate -> connected sensor
        # A zone whose last preset was sent while a heater was offline: re-send it
        # once every heater is back online.
        self._zone_offline_apply: dict[str, bool] = {ZONE_A: False, ZONE_B: False}
        self._shared_offline_apply = False

        # Cached calendar look-ahead (refreshed on a slower cadence).
        self.cal_window: dict[str, bool] = {ZONE_A: False, ZONE_B: False}
        self.cal_title: dict[str, str] = {ZONE_A: "", ZONE_B: ""}
        self.water_window = False

        # Optimum-start learning: an in-flight warm-up sample per zone
        # (started-at, start temperature, ticks-with-fans-running, total
        # ticks, O1 wattage sum, wattage reading count), an in-flight
        # cool-off sample per zone (started-at, start temperature, outdoor
        # reading sum, outdoor reading count — the average gap normalises the
        # observed loss), and the last comfort target seen on the zone's own
        # heater (used as the office target, which the integration does not
        # otherwise know).
        self._warmup_start: dict[
            str, tuple[datetime, float, int, int, float, int] | None
        ] = {
            ZONE_A: None,
            ZONE_B: None,
        }
        self._cooloff_start: dict[
            str, tuple[datetime, float, float, int, int, int] | None
        ] = {
            ZONE_A: None,
            ZONE_B: None,
        }
        self._zone_comfort_target: dict[str, float | None] = {ZONE_A: None, ZONE_B: None}

        # Rolling audit trail of decisions, learning samples and outcomes,
        # persisted with the snapshot and exported via the diagnostics
        # download so the tuning constants can be checked against the hut's
        # real behaviour. The trace records the readings behind the
        # decisions (a week at 15-minute spacing) alongside the events.
        self.audit = AuditLog()
        self.trace = Trace()
        # The inputs behind the most recent lead computation per zone, stashed
        # so the pre-heat-start audit event can carry them.
        self._last_lead_calc: dict[str, dict[str, Any]] = {}
        # Previous running state per calendar; None = not yet observed, so a
        # restart mid-booking does not audit a phantom booking start.
        self._cal_running_prev: dict[str, bool | None] = {ZONE_A: None, ZONE_B: None}
        # Why each zone's desired preset is what it is (the rung of the
        # priority ladder that decided it), stashed by _desired_zone/_shared
        # so preset audit events can say WHY, not just what.
        self._preset_reason: dict[str, str] = {}

        # Durable state (safety latches and long clocks survive a restart).
        self._store: Store = Store(hass, 1, f"{DOMAIN}_{entry.entry_id}")

        self._unsubs: list = []
        self._started = False
        self._reconciling = False
        self._reconcile_pending = False
        self._debounce_cancel = None

    # ------------------------------------------------------------------
    # Registration API used by the input platforms
    # ------------------------------------------------------------------
    def register_number(self, key: str, entity: Any) -> None:
        self._numbers[key] = entity

    def register_switch(self, key: str, entity: Any) -> None:
        self._switches[key] = entity

    def register_select(self, key: str, entity: Any) -> None:
        self._selects[key] = entity

    def register_text(self, key: str, entity: Any) -> None:
        self._texts[key] = entity

    # ------------------------------------------------------------------
    # Value helpers for the tunable inputs
    # ------------------------------------------------------------------
    def number(self, key: str) -> float:
        entity = self._numbers.get(key)
        if entity is not None and entity.native_value is not None:
            return float(entity.native_value)
        return float(NUMBER_DEFS[key][3])

    def switch_on(self, key: str, default: bool = False) -> bool:
        entity = self._switches.get(key)
        if entity is not None:
            return bool(entity.is_on)
        return default

    def boost_minutes(self) -> int:
        entity = self._selects.get("boost_duration")
        option = entity.current_option if entity is not None else "60 min"
        digits = "".join(ch for ch in (option or "") if ch.isdigit())
        return int(digits) if digits else 60

    def eco_keywords(self) -> list[str]:
        entity = self._texts.get("eco_keywords")
        raw = entity.native_value if entity is not None else ""
        return [kw.strip().lower() for kw in (raw or "").split(",") if kw.strip()]

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    @callback
    def async_start(self) -> None:
        """Begin listening for events and schedule the first reconcile."""
        watched: list[str] = []
        for key in MOTION_AREAS.values():
            if ent := self.config.get(key):
                watched.append(ent)
        for key in (
            CONF_ZONE_A_DOORS,
            CONF_ZONE_A_WINDOWS,
            CONF_ZONE_B_DOORS,
            CONF_ZONE_B_WINDOWS,
            CONF_SHARED_WINDOWS,
        ):
            watched.extend(self._as_list(self.config.get(key)))
        for key in (
            CONF_INTERNAL_DOOR,
            CONF_CALENDAR_HALL,
            CONF_CALENDAR_OFFICE,
            CONF_ALARM_MAIN,
            CONF_ALARM_OFFICE,
            CONF_WATER_SWITCH,
            CONF_CEILING_TEMP,
            CONF_FLOOR_TEMP,
            CONF_FAN_MASTER,
            CONF_FAN_DIRECTION,
            CONF_FAN_FAULT,
        ):
            if ent := self.config.get(key):
                watched.append(ent)
        # Rointe Effective Power sensors drive the heat-demand signal.
        watched.extend(self._as_list(self.config.get(CONF_ROINTE_POWER)))

        motion_entities = {
            self.config[key]: area
            for area, key in MOTION_AREAS.items()
            if self.config.get(key)
        }

        @callback
        def _handle_state_event(event: Event) -> None:
            entity_id = event.data.get("entity_id")
            new_state = event.data.get("new_state")
            if entity_id in motion_entities and new_state is not None and new_state.state == "on":
                self._feed_motion(motion_entities[entity_id], dt_util.utcnow())
            self.async_request_reconcile()

        if watched:
            self._unsubs.append(
                async_track_state_change_event(self.hass, watched, _handle_state_event)
            )

        # Periodic reconcile handles timers (openings, boost expiry, motion timeout).
        self._unsubs.append(
            async_track_time_interval(self.hass, self._async_tick, RECONCILE_INTERVAL)
        )
        # Re-evaluate the seasonal lockout every hour on the hour, so a changing
        # forecast (or a fresh setup) is reflected within the hour rather than
        # waiting for a single daily check.
        self._unsubs.append(
            async_track_time_change(
                self.hass, self._async_seasonal_time, minute=0, second=0
            )
        )

        async def _first_run(_now: datetime) -> None:
            await self._async_restore_state()
            # Load the calendar and forecast BEFORE accepting reconciles, so a
            # sensor event arriving mid-load cannot apply presets computed
            # from empty calendar data (briefly icing a zone mid-booking).
            await self._async_refresh_calendars()
            await self._async_seasonal_check()
            self._started = True
            await self.async_reconcile()

        self._unsubs.append(async_call_later(self.hass, STARTUP_DELAY, _first_run))

    @callback
    def async_stop(self) -> None:
        """Cancel all listeners."""
        if self._debounce_cancel is not None:
            self._debounce_cancel()
            self._debounce_cancel = None
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()

    # ------------------------------------------------------------------
    # Reconcile scheduling
    # ------------------------------------------------------------------
    @callback
    def async_request_reconcile(self) -> None:
        """Debounce a reconcile a second into the future to coalesce bursts."""
        if not self._started:
            return
        if self._debounce_cancel is not None:
            self._debounce_cancel()

        async def _run(_now: datetime) -> None:
            self._debounce_cancel = None
            await self.async_reconcile()

        self._debounce_cancel = async_call_later(self.hass, 1, _run)

    async def _async_tick(self, _now: datetime) -> None:
        if not self._started:
            return
        # Refresh the calendar look-ahead roughly every five minutes.
        minute = dt_util.now().minute
        if minute % 5 == 0:
            await self._async_refresh_calendars()
        await self.async_reconcile()

    async def _async_seasonal_time(self, _now: datetime) -> None:
        await self._async_seasonal_check()
        await self.async_reconcile()

    # ------------------------------------------------------------------
    # State reading helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _as_list(value: Any) -> list[str]:
        if not value:
            return []
        if isinstance(value, str):
            return [value]
        return list(value)

    def _is_on(self, entity_id: str | None) -> bool:
        if not entity_id:
            return False
        state = self.hass.states.get(entity_id)
        return state is not None and state.state == "on"

    def _any_on(self, entity_ids: list[str]) -> bool:
        return any(self._is_on(e) for e in entity_ids)

    def _now(self) -> datetime:
        return dt_util.utcnow()

    def _motion_recent(self, area: str, timeout_min: float) -> bool:
        last = self.last_motion.get(area)
        if last is None:
            return False
        return (self._now() - last).total_seconds() <= timeout_min * 60

    def _motion_recent_any(self, timeout_min: float) -> bool:
        return any(self._motion_recent(a, timeout_min) for a in MOTION_AREAS)

    def _feed_motion(self, area: str, now: datetime) -> None:
        """Stamp motion in an area, auditing only a genuine fresh arrival.

        A PIR re-firing while the area is still occupied (another trip within
        the occupancy timeout) would flood the bounded audit log during a
        busy session, so it just refreshes the timestamp; a trip after the
        area has gone quiet is a new arrival worth a `motion` event — the
        evidence that the PIRs are alive, which motion otherwise leaves only
        indirectly (when it moves a preset or the fans).
        """
        prev = self.last_motion.get(area)
        timeout = self.number("motion_timeout_minutes")
        if prev is None or (now - prev).total_seconds() > timeout * 60:
            self.audit.record("motion", now, area=area)
        self.last_motion[area] = now

    def _any_opening_open(self) -> bool:
        """Any mapped opening contact at all (used by the breeze override)."""
        for key in (
            CONF_ZONE_A_DOORS,
            CONF_ZONE_A_WINDOWS,
            CONF_ZONE_B_DOORS,
            CONF_ZONE_B_WINDOWS,
            CONF_SHARED_WINDOWS,
        ):
            if self._any_on(self._as_list(self.config.get(key))):
                return True
        return self._is_on(self.config.get(CONF_INTERNAL_DOOR))

    def _exterior_open(self, zone: str) -> bool:
        doors = self._as_list(self.config.get(ZONE_DOORS[zone]))
        windows = self._as_list(self.config.get(ZONE_WINDOWS[zone]))
        return self._any_on(doors) or self._any_on(windows)

    def _open_held(self, group_key: str, entity_ids: list[str], threshold_min: float) -> bool:
        """Return True if the group has been continuously open >= threshold."""
        now = self._now()
        if self._any_on(entity_ids):
            if self.open_since.get(group_key) is None:
                self.open_since[group_key] = now
            elapsed = (now - self.open_since[group_key]).total_seconds()
            return elapsed >= threshold_min * 60
        self.open_since[group_key] = None
        return False

    # ------------------------------------------------------------------
    # Calendar / weather refresh
    # ------------------------------------------------------------------
    async def _async_refresh_calendars(self) -> None:
        water_preheat = int(self.number("water_preheat_minutes"))
        cap = int(round(self.number("preheat_minutes")))
        now = dt_util.now()
        for zone in (ZONE_A, ZONE_B):
            cal = self.config.get(ZONE_CALENDAR[zone])
            if not cal:
                continue
            if self._is_on(cal):
                state = self.hass.states.get(cal)
                self.cal_window[zone] = True
                self.cal_title[zone] = (
                    (state.attributes.get("message") or "").lower() if state else ""
                )
                continue
            # Look ahead as far as the pre-heat cap, then compute the actual
            # lead for the specific event found: its own target (an ECO event
            # pre-heats to the lower eco setpoint) and the idle gap until it
            # starts (during which the room keeps cooling).
            events = await self._async_calendar_events(cal, cap)
            if events is None:
                # Calendar service blip: keep the previous window and title.
                # Dropping to "no window" on a transient error would cancel an
                # active pre-heat and release a manual hold mid-booking.
                continue
            if not events:
                self.cal_window[zone] = False
                self.cal_title[zone] = ""
                continue
            first = events[0]
            start = self._parse_event_start(first.get("start"))
            self.cal_title[zone] = (first.get("summary", "") or "").lower()
            eco = any(kw in self.cal_title[zone] for kw in self.eco_keywords())
            gap_min: float | None = None
            if start is not None:
                try:
                    gap_min = max((start - now).total_seconds() / 60, 0.0)
                except TypeError:  # naive/aware mismatch from an odd calendar
                    gap_min = None
            if gap_min is None:
                # The event is inside the cap window but its start could not
                # be read: err on the warm side and pre-heat now.
                if not self.cal_window[zone]:
                    self.audit.record(
                        "preheat_start", self._now(), zone=zone, reason="unreadable_start"
                    )
                self.cal_window[zone] = True
                continue
            lead = self._zone_preheat_minutes(zone, eco=eco, gap_hours=gap_min / 60)
            window = gap_min <= lead
            if window and not self.cal_window[zone]:
                self.audit.record(
                    "preheat_start",
                    self._now(),
                    zone=zone,
                    **(self._last_lead_calc.get(zone) or {}),
                )
            self.cal_window[zone] = window

        water = False
        for zone in (ZONE_A, ZONE_B):
            cal = self.config.get(ZONE_CALENDAR[zone])
            if not cal:
                continue
            in_window, _ = await self._async_calendar_window(cal, water_preheat)
            water = water or in_window or self._is_on(cal)
        self.water_window = water

    async def _async_calendar_events(self, cal: str, minutes: int) -> list[dict] | None:
        """Events on a calendar within the next `minutes`; None on error."""
        start = dt_util.now()
        end = start + timedelta(minutes=minutes)
        try:
            response = await self.hass.services.async_call(
                "calendar",
                "get_events",
                {
                    "start_date_time": start.strftime("%Y-%m-%d %H:%M:%S"),
                    "end_date_time": end.strftime("%Y-%m-%d %H:%M:%S"),
                },
                target={"entity_id": cal},
                blocking=True,
                return_response=True,
            )
        except Exception as err:  # noqa: BLE001 - calendar may be unavailable
            _LOGGER.debug("calendar.get_events failed for %s: %s", cal, err)
            return None
        return (response or {}).get(cal, {}).get("events", []) if response else []

    @staticmethod
    def _parse_event_start(value: Any) -> datetime | None:
        if not value:
            return None
        if isinstance(value, datetime):
            parsed = value
        else:
            try:
                parsed = datetime.fromisoformat(str(value))
            except ValueError:
                return None
        if parsed.tzinfo is None:
            # All-day events come back date-only -> naive midnight. Anchor to
            # local time so the gap arithmetic works instead of raising and
            # falling into the maximum-lead path.
            parsed = parsed.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
        return parsed

    async def _async_calendar_window(self, cal: str, minutes: int) -> tuple[bool, str]:
        """Return (event within window?, first event summary)."""
        events = await self._async_calendar_events(cal, minutes)
        if events is None:
            return self._is_on(cal), ""
        if events:
            return True, events[0].get("summary", "") or ""
        return False, ""

    # ------------------------------------------------------------------
    # Adaptive pre-heat (optimum start)
    # ------------------------------------------------------------------
    def _zone_climate_temps(self, zone: str) -> list[float]:
        """All readable room temperatures from a zone's own heaters."""
        vals: list[float] = []
        for climate in self._as_list(self.config.get(ZONE_CLIMATES[zone])):
            st = self.hass.states.get(climate)
            if st is None or st.state in ("unavailable", "unknown"):
                continue
            temp = st.attributes.get("current_temperature")
            try:
                if temp is not None:
                    vals.append(float(temp))
            except (TypeError, ValueError):
                continue
        return vals

    def _zone_room_temp(self, zone: str, coldest: bool = False) -> float | None:
        """Room temperature reported by a zone's own heaters.

        ``coldest=True`` returns the lowest reading instead of the average:
        the hall units disagree by several degrees along the 20 m room, and
        for "will the room be warm enough?" questions (pre-heat sizing) the
        coldest reading is the truer measure of the far end. The average
        stays right for the fan ΔT reference and the learning, where
        stability against a single odd sensor matters more.
        """
        vals = self._zone_climate_temps(zone)
        if coldest and vals:
            return min(vals)
        if zone == ZONE_A:
            # The hall average shares the fan logic's floor reading (explicit
            # floor sensor if mapped, else the hall Rointes' average).
            return self._floor_temp()
        return sum(vals) / len(vals) if vals else None

    @property
    def hall_temp_spread(self) -> float | None:
        """Max-minus-min across the hall heaters' readings (diagnostic).

        Shows how patchy the hall is side-to-side; expected to collapse to
        under ~1 °C once the destratification fans mix the room. None with
        fewer than two readable heaters.
        """
        vals = self._zone_climate_temps(ZONE_A)
        if len(vals) < 2:
            return None
        return max(vals) - min(vals)

    def _outdoor_temp(self) -> float | None:
        weather = self.config.get(CONF_WEATHER)
        if not weather:
            return None
        st = self.hass.states.get(weather)
        if st is None:
            return None
        try:
            temp = st.attributes.get("temperature")
            return float(temp) if temp is not None else None
        except (TypeError, ValueError):
            return None

    def _zone_target(self, zone: str) -> float:
        """The comfort temperature a pre-heat is aiming for.

        The hall target is the integration's own slider (it is pushed onto the
        heaters). The office comfort setpoint lives on the Rointe itself, so
        the last target seen while the office was actually in comfort is
        cached and used; until one has been seen, the hall slider is the
        best available proxy.
        """
        if zone == ZONE_A:
            return self.number("hall_comfort_temp")
        cached = self._zone_comfort_target.get(zone)
        return cached if cached is not None else self.number("hall_comfort_temp")

    def _zone_preheat_minutes(
        self, zone: str, eco: bool = False, gap_hours: float | None = None
    ) -> int:
        """Adaptive pre-heat lead for a zone's next event, capped by the slider.

        eco: the event matches an ECO keyword, so the pre-heat aims at the
        lower eco-low setpoint instead of comfort (the same target the
        reconciler will push when the event runs). gap_hours: time until the
        event starts, when known — the learned heat-loss rate then predicts
        how much further the room will cool before the pre-heat begins.
        """
        target = self.number("hall_eco_low_temp") if eco else self._zone_target(zone)
        rate = self._prediction_rate(zone)
        # Size the pre-heat for the coldest reading, not the average: the
        # warm end's heater must not cut the lead short for the cold end.
        indoor = self._zone_room_temp(zone, coldest=True)
        outdoor = self._outdoor_temp()
        loss_pct = self.number(f"{zone}_heatloss_pct")
        minutes = required_lead_minutes(
            rate=rate,
            indoor=indoor,
            target=target,
            outdoor=outdoor,
            max_minutes=self.number("preheat_minutes"),
            gap_hours=gap_hours,
            cool_k=loss_pct / 100,
        )
        lead = int(round(minutes))
        # Stash the inputs so the caller can audit the computation that
        # actually opens a pre-heat window (recording every 5-minute
        # recalculation would drown the log).
        self._last_lead_calc[zone] = {
            "eco": eco,
            "gap_min": None if gap_hours is None else gap_hours * 60,
            "lead_min": lead,
            "rate": rate,
            "indoor_coldest": indoor,
            "target": target,
            "outdoor": outdoor,
            "loss_pct": loss_pct,
        }
        return lead

    def _prediction_rate(self, zone: str) -> float:
        """The learned rate to predict a warm-up with.

        The fan-assisted hall rate is preferred when the fans are expected to
        help, but only once it has actually been trained: at its untouched
        fail-safe seed (MAX_RATE) it would pin every lead at the cap forever
        while the observations were all landing in the base rate. Until then,
        fall back to whichever knowledge exists.
        """
        key = self._warmup_rate_key(zone)
        rate = self.number(key)
        if key == "zone_a_warmup_rate_fans" and rate >= MAX_RATE:
            rate = min(rate, self.number("zone_a_warmup_rate"))
        return rate

    def _warmup_rate_key(self, zone: str, assisted: bool | None = None) -> str:
        """Which learned warm-up rate applies to a zone.

        The hall keeps two: with and without the destratification fans
        running, because the fans materially change warm-up speed.
        ``assisted=None`` asks for the rate to *predict* with (will the fans
        help the next warm-up?); a bool records which rate an *observed*
        warm-up should update.
        """
        if zone != ZONE_A or not self.config.get(CONF_FAN_MASTER):
            return f"{zone}_warmup_rate"
        if assisted is None:
            assisted = self.switch_on("fans_enabled", default=True) and not self._summer_active()
        return "zone_a_warmup_rate_fans" if assisted else "zone_a_warmup_rate"

    def _fans_running(self) -> bool:
        """Whether the fans are genuinely moving air right now.

        Master on, and — when the O1 power sensor is mapped — actually drawing
        fan-scale power: a closed master with the transformer dial at zero
        moves no air and must not count as fan-assisted in the learning.
        """
        master = self.config.get(CONF_FAN_MASTER)
        if not master or not self._is_on(master):
            return False
        power = self._o1_watts()
        if power is not None:
            return power > FAN_RUNNING_MIN_WATTS
        # No fresh power reading: trust the commanded master state.
        return True

    def _o1_watts(self) -> float | None:
        """Fresh O1 power reading, or None.

        Beyond the running/not gate, the wattage encodes the transformer
        dial's tap — a manual control the integration cannot see otherwise —
        so it is recorded with warm-up samples, fan events and the trace to
        show whether dial changes are perturbing the learned rates.
        """
        o1 = self.config.get(CONF_FAN_O1_POWER)
        if not o1 or self._stale(o1, self.number("fan_sensor_stale_minutes")):
            return None
        return self._num_state(o1)

    def _update_warmup_learning(self) -> None:
        """Time real comfort warm-ups and fold them into the learned rates.

        A sample starts when a zone enters comfort while measurably below
        target, and ends when the target is reached or comfort ends; the
        observed minutes-per-degree updates the zone's learned rate (EWMA,
        clamped — see preheat.py). Aborted warm-ups with too little rise are
        ignored, so an opening pause or a cloud blip cannot poison the rate.
        """
        now = self._now()
        for zone in (ZONE_A, ZONE_B):
            comfort = (
                self.applied[zone] == PRESET_COMFORT
                and not self.opening_ice[zone]
                and self.switch_on(f"{zone}_automation_enabled", default=True)
            )
            temp = self._zone_room_temp(zone)

            # Cache the zone's real comfort target from its own heater while
            # it is actually in comfort (needed for the office, see _zone_target).
            if comfort:
                climates = self._as_list(self.config.get(ZONE_CLIMATES[zone]))
                if climates and (st := self.hass.states.get(climates[0])) is not None:
                    try:
                        target_attr = st.attributes.get("temperature")
                        if target_attr is not None and 15.0 <= float(target_attr) <= 30.0:
                            self._zone_comfort_target[zone] = float(target_attr)
                    except (TypeError, ValueError):
                        pass

            target = self._zone_target(zone)
            sample = self._warmup_start[zone]

            if sample is None:
                if comfort and temp is not None and temp < target - 0.5:
                    fans = 1 if self._fans_running() else 0
                    w = self._o1_watts() if zone == ZONE_A else None
                    self._warmup_start[zone] = (
                        now,
                        temp,
                        fans,
                        1,
                        w or 0.0,
                        1 if w is not None else 0,
                    )
                continue

            started, start_temp, fan_ticks, ticks, watt_sum, watt_n = sample
            done = comfort and temp is not None and temp >= target
            if comfort and not done:
                # Still warming (or temp reading lost: wait). Keep tallying
                # whether the fans are assisting and how hard (the O1 wattage
                # encodes the manual dial tap).
                fan_ticks += 1 if self._fans_running() else 0
                w = self._o1_watts() if zone == ZONE_A else None
                if w is not None:
                    watt_sum += w
                    watt_n += 1
                self._warmup_start[zone] = (
                    started,
                    start_temp,
                    fan_ticks,
                    ticks + 1,
                    watt_sum,
                    watt_n,
                )
                continue

            # Warm-up finished (target reached) or ended early (preset left
            # comfort): fold the observation into the applicable rate — the
            # fan-assisted one when the fans ran for most of the warm-up.
            self._warmup_start[zone] = None
            if temp is None:
                self.audit.record(
                    "warmup_discarded",
                    now,
                    zone=zone,
                    reason="reading_lost",
                    minutes=(now - started).total_seconds() / 60,
                )
                continue
            minutes = (now - started).total_seconds() / 60
            rise = temp - start_temp
            assisted = fan_ticks * 2 >= ticks
            rate_key = self._warmup_rate_key(zone, assisted=assisted)
            old_rate = self.number(rate_key)
            new_rate = updated_rate(old_rate, minutes, rise)
            self.audit.record(
                "warmup_sample",
                now,
                zone=zone,
                rate_key=rate_key,
                minutes=minutes,
                rise=rise,
                start_temp=start_temp,
                end_temp=temp,
                fan_ticks=fan_ticks,
                ticks=ticks,
                o1_avg_w=(watt_sum / watt_n) if watt_n else None,
                reached_target=done,
                accepted=rise >= MIN_SAMPLE_RISE and minutes >= MIN_SAMPLE_MINUTES,
                old_rate=old_rate,
                new_rate=new_rate,
            )
            entity = self._numbers.get(rate_key)
            write = getattr(entity, "write_value", None)
            if write is not None and new_rate != old_rate:
                write(new_rate)

    def _update_cooloff_learning(self) -> None:
        """Measure how fast an unheated zone loses heat (retention learning).

        A sample runs while a zone sits at ice (heating effectively off): a
        real drop over a real duration updates the zone's learned heat-loss
        rate and re-anchors the sample. A temperature *rise* while unheated
        (solar gain, heat leaking from another zone) re-anchors without
        learning, so warmth can never be mistaken for insulation.
        """
        now = self._now()
        for zone in (ZONE_A, ZONE_B):
            # A held-open door/window is ventilation loss, not fabric loss:
            # discard any in-flight sample rather than learning it as
            # insulation quality.
            if self.opening_ice[zone]:
                if self._cooloff_start[zone] is not None:
                    self.audit.record("cooloff_discarded", now, zone=zone, reason="opening")
                self._cooloff_start[zone] = None
                continue

            cooling = self.applied[zone] == PRESET_ICE
            temp = self._zone_room_temp(zone)
            outdoor = self._outdoor_temp()
            sample = self._cooloff_start[zone]

            def _anchor(anchor_temp: float) -> tuple[datetime, float, float, int, int, int]:
                return (
                    now,
                    anchor_temp,
                    outdoor if outdoor is not None else 0.0,
                    1 if outdoor is not None else 0,
                    1 if self._fans_running() else 0,
                    1,
                )

            if sample is None:
                if cooling and temp is not None:
                    self._cooloff_start[zone] = _anchor(temp)
                continue

            started, start_temp, out_sum, out_n, fan_ticks, ticks = sample
            # Accumulate the outdoor reading every tick: the sample's average
            # gap is what normalises the observed loss into the constant. The
            # fan tally rides along because a fan-mixed cool-off measurably
            # differs from a still one (2026-07-11 sealed test: mixing roughly
            # halved the gap-normalised loss) — recorded, not yet acted on.
            if outdoor is not None:
                out_sum += outdoor
                out_n += 1
            fan_ticks += 1 if self._fans_running() else 0
            ticks += 1
            if not cooling or temp is None:
                # Heating resumed (or reading lost): fold in whatever partial
                # drop there was and stop sampling.
                self._cooloff_start[zone] = None
                if temp is not None:
                    hours = (now - started).total_seconds() / 3600
                    self._fold_cooloff(
                        zone,
                        hours,
                        start_temp - temp,
                        start_temp,
                        temp,
                        out_sum,
                        out_n,
                        fan_ticks,
                        ticks,
                    )
                continue

            if temp > start_temp + 0.3:
                self._cooloff_start[zone] = _anchor(temp)  # gaining, not losing
                continue
            self._cooloff_start[zone] = (started, start_temp, out_sum, out_n, fan_ticks, ticks)
            drop = start_temp - temp
            hours = (now - started).total_seconds() / 3600
            # Roll the window ONLY when the sample is long enough to be
            # accepted: re-anchoring on a rejected (too-short) sample would
            # create a dead zone where fast heat loss (reaching the drop
            # trigger in under the minimum duration) could never be learned.
            if drop >= MIN_COOL_SAMPLE_DROP and hours >= MIN_COOL_SAMPLE_HOURS:
                self._fold_cooloff(
                    zone, hours, drop, start_temp, temp, out_sum, out_n, fan_ticks, ticks
                )
                self._cooloff_start[zone] = _anchor(temp)  # rolling window

    def _fold_cooloff(
        self,
        zone: str,
        hours: float,
        drop: float,
        start_temp: float,
        end_temp: float,
        out_sum: float,
        out_n: int,
        fan_ticks: int,
        ticks: int,
    ) -> None:
        key = f"{zone}_heatloss_pct"
        current = self.number(key)
        if current <= 0:
            return  # prediction disabled by the user; leave it that way
        if out_n == 0:
            # No outdoor reading during the whole sample: the gap — and so
            # the loss constant — is unknowable. Never guess.
            self.audit.record(
                "cooloff_sample",
                self._now(),
                zone=zone,
                hours=hours,
                drop=drop,
                accepted=False,
                reason="no_outdoor",
                fan_ticks=fan_ticks,
                ticks=ticks,
            )
            return
        gap = (start_temp + end_temp) / 2 - out_sum / out_n
        k = current / 100
        new_k = updated_cooling_k(k, hours, drop, gap)
        new = current if new_k == k else new_k * 100
        self.audit.record(
            "cooloff_sample",
            self._now(),
            zone=zone,
            hours=hours,
            drop=drop,
            gap=gap,
            accepted=(
                drop >= MIN_COOL_SAMPLE_DROP
                and hours >= MIN_COOL_SAMPLE_HOURS
                and gap >= MIN_COOL_SAMPLE_GAP
            ),
            old_pct=current,
            new_pct=new,
            fan_ticks=fan_ticks,
            ticks=ticks,
        )
        entity = self._numbers.get(key)
        write = getattr(entity, "write_value", None)
        if write is not None and new != current:
            write(new)

    async def _async_seasonal_check(self) -> None:
        weather = self.config.get(CONF_WEATHER)
        if not weather:
            return
        threshold = self.number("seasonal_lockout_temp")
        try:
            response = await self.hass.services.async_call(
                "weather",
                "get_forecasts",
                {"type": "daily"},
                target={"entity_id": weather},
                blocking=True,
                return_response=True,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("weather.get_forecasts failed for %s: %s", weather, err)
            return
        forecast = (response or {}).get(weather, {}).get("forecast", [])[:3]
        if not forecast:
            return

        realfeel = 99.0
        if rf := self.config.get(CONF_REALFEEL):
            state = self.hass.states.get(rf)
            try:
                realfeel = float(state.state) if state else 99.0
            except (TypeError, ValueError):
                realfeel = 99.0

        avg, warm, cold = self._lockout_decision(forecast, threshold, realfeel)
        if avg is None:
            return

        if warm and not self.seasonal_lockout:
            self.seasonal_lockout = True
            self.audit.record(
                "seasonal",
                self._now(),
                engaged=True,
                avg=avg,
                threshold=threshold,
                realfeel=realfeel if realfeel != 99.0 else None,
            )
            persistent_notification.async_create(
                self.hass,
                (
                    f"The next 3 days average {avg:.1f}°C (lockout threshold "
                    f"{threshold:.0f}°C). Heating is locked out for the season. "
                    "A Boost still works if it is genuinely needed."
                ),
                title="🏕 Scout Hut – Seasonal lockout engaged",
                notification_id=NOTIFY_SEASONAL,
            )
        elif cold and self.seasonal_lockout:
            self.seasonal_lockout = False
            self.audit.record(
                "seasonal",
                self._now(),
                engaged=False,
                avg=avg,
                threshold=threshold,
                realfeel=realfeel if realfeel != 99.0 else None,
            )
            persistent_notification.async_dismiss(self.hass, NOTIFY_SEASONAL)

    @staticmethod
    def _lockout_decision(
        forecast: list[dict], threshold: float, realfeel: float
    ) -> tuple[float | None, bool, bool]:
        """Decide the lockout from the 3-day average mean daily temperature.

        Averaging the mean daily temperature ((high + overnight low) / 2) — rather
        than requiring every high AND every low to clear the threshold — means a
        warm season still locks out even when nights dip, which is both the
        sensible real-world behaviour and what the control has always been
        labelled ("3-day average"). Release requires the average to fall
        SEASONAL_RELEASE_BAND below the threshold (or a cold-snap RealFeel), so
        the lockout cannot flap while the forecast hovers at the threshold.
        Returns (avg, engage?, release?).
        """

        def _f(value: Any) -> float | None:
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        means: list[float] = []
        for day in forecast[:3]:
            high = _f(day.get("temperature"))
            low = _f(day.get("templow"))
            if high is None and low is None:
                continue
            if low is None:
                means.append(high)
            elif high is None:
                means.append(low)
            else:
                means.append((high + low) / 2)
        if not means:
            return None, False, False
        avg = sum(means) / len(means)
        # A genuine cold snap (RealFeel well below the threshold) releases the
        # lockout; a mild summer night a degree under it does not. Engage
        # excludes every release condition so the two can never both be true —
        # otherwise the lockout would flap on every hourly check.
        release = (
            avg <= threshold - SEASONAL_RELEASE_BAND
            or realfeel < threshold - SEASONAL_SNAP_BAND
        )
        engage = avg >= threshold and realfeel >= threshold - SEASONAL_SNAP_BAND
        return avg, engage, release

    # ------------------------------------------------------------------
    # Boost API (called by the button platform)
    # ------------------------------------------------------------------
    async def async_boost(self, zone: str) -> None:
        self.boost_until[zone] = self._now() + timedelta(minutes=self.boost_minutes())
        await self.async_reconcile()

    async def async_cancel_boost(self, zone: str) -> None:
        self.boost_until[zone] = None
        await self.async_reconcile()

    def boost_active(self, zone: str) -> bool:
        until = self.boost_until.get(zone)
        return until is not None and self._now() < until

    async def async_seasonal_recheck(self) -> None:
        """Re-evaluate the seasonal lockout (e.g. after a threshold change)."""
        await self._async_seasonal_check()
        await self.async_reconcile()

    async def async_hall_temps_changed(self) -> None:
        """Re-push hall temperatures when a temperature slider changes."""
        if self.applied[ZONE_A] in (PRESET_COMFORT, PRESET_ECO):
            await self._async_push_hall_temps(eco_low=self._eco_keyword_active(ZONE_A))
            await self._async_set_preset(ZONE_A, self.applied[ZONE_A], force=True)

    def _state_snapshot(self) -> dict[str, Any]:
        """Durable state: safety latches and clocks that must survive a restart.

        Without this, a hut whose HA restarts weekly would never run the water
        hygiene cycle, a latched fan fault would silently self-re-arm, and a
        restart mid-booking would drop a leader's manual hold.
        """
        return {
            "water_last_hot": self.water_last_hot.isoformat() if self.water_last_hot else None,
            "fan_fault_latched": self.fan_fault_latched,
            "boost_until": {
                zone: until.isoformat() if until else None
                for zone, until in self.boost_until.items()
            },
            "manual_hold": dict(self.manual_hold),
            # Restart hardening: the anti-short-cycle timers and the seasonal
            # flag survive a reload, so a restart cannot stutter the fans or
            # re-announce the lockout.
            "seasonal_lockout": self.seasonal_lockout,
            "fan_last_on": self.fan_last_on.isoformat() if self.fan_last_on else None,
            "fan_last_off": self.fan_last_off.isoformat() if self.fan_last_off else None,
            "audit": self.audit.to_list(),
            "trace": self.trace.to_list(),
        }

    async def _async_restore_state(self) -> None:
        try:
            data = await self._store.async_load()
        except Exception as err:  # noqa: BLE001 - corrupt store must not block startup
            _LOGGER.warning("Could not restore saved state: %s", err)
            return
        if not data:
            return

        def _dt(value: Any) -> datetime | None:
            try:
                return datetime.fromisoformat(value) if value else None
            except (TypeError, ValueError):
                return None

        if (ts := _dt(data.get("water_last_hot"))) is not None:
            self.water_last_hot = ts
        self.fan_fault_latched = bool(data.get("fan_fault_latched", False))
        for zone in (ZONE_A, ZONE_B):
            until = _dt((data.get("boost_until") or {}).get(zone))
            if until is not None and until > self._now():
                self.boost_until[zone] = until
            if (data.get("manual_hold") or {}).get(zone):
                self.manual_hold[zone] = True
        self.seasonal_lockout = bool(data.get("seasonal_lockout", False))
        self.fan_last_on = _dt(data.get("fan_last_on"))
        self.fan_last_off = _dt(data.get("fan_last_off"))
        self.audit.load(data.get("audit"))
        self.trace.load(data.get("trace"))

    def diagnostics_data(self) -> dict[str, Any]:
        """Everything needed to audit the controller offline.

        Served by the integration's diagnostics download (integration page →
        ⋮ → Download diagnostics). Contains no credentials — entity ids,
        tunable values against their defaults, learned rates, a live reading
        snapshot and the rolling audit-event log — so the tuning constants
        can be re-derived from the hut's real behaviour.
        """

        def _iso(ts: datetime | None) -> str | None:
            return ts.isoformat(timespec="seconds") if ts else None

        numbers = {
            key: {"value": self.number(key), "default": float(defn[3])}
            for key, defn in NUMBER_DEFS.items()
        }
        switches = {
            key: {"value": self.switch_on(key, default=default), "default": default}
            for key, default in SWITCH_DEFS.items()
        }

        zones: dict[str, Any] = {}
        for zone in (ZONE_A, ZONE_B):
            heaters: dict[str, Any] = {}
            for climate in self._as_list(self.config.get(ZONE_CLIMATES[zone])):
                st = self.hass.states.get(climate)
                heaters[climate] = {
                    "state": st.state if st else None,
                    "temp": (st.attributes.get("current_temperature") if st else None),
                    "preset": (st.attributes.get("preset_mode") if st else None),
                    "online": self._climate_online(climate),
                }
            zones[zone] = {
                "heaters": heaters,
                "average": self._zone_room_temp(zone),
                "coldest": self._zone_room_temp(zone, coldest=True),
            }

        stale_min = self.number("fan_sensor_stale_minutes")
        power = {
            sensor: {
                "value": self._num_state(sensor),
                "stale": self._stale(sensor, stale_min),
            }
            for sensor in self._power_sensors()
        }

        return {
            "generated": _iso(self._now()),
            "config": dict(self.config),
            "tunables": {
                "numbers": numbers,
                "switches": switches,
                "boost_minutes": self.boost_minutes(),
                "eco_keywords": self.eco_keywords(),
            },
            "learned": {
                "office_comfort_target": self._zone_comfort_target.get(ZONE_B),
                "warmup_in_flight": {
                    zone: _iso(sample[0]) if sample else None
                    for zone, sample in self._warmup_start.items()
                },
                "cooloff_in_flight": {
                    zone: _iso(sample[0]) if sample else None
                    for zone, sample in self._cooloff_start.items()
                },
                "last_lead_calc": self._last_lead_calc,
            },
            "state": {
                "applied": dict(self.applied),
                "expected": dict(self.expected_preset),
                "manual_hold": dict(self.manual_hold),
                "opening_ice": dict(self.opening_ice),
                "boost_until": {z: _iso(t) for z, t in self.boost_until.items()},
                "seasonal_lockout": self.seasonal_lockout,
                "summer_active": self._summer_active(),
                "cal_window": dict(self.cal_window),
                "cal_title": dict(self.cal_title),
                "fan": {
                    "on": self.fan_on,
                    "mode": self.fan_mode,
                    "direction": self.fan_direction,
                    "fault": self.fan_fault_effective,
                    "sensor_stale": self.fan_sensor_stale,
                    "overheated": self.fan_overheated,
                    "breeze_hot": self.fan_breeze_hot,
                    "mix": self.fan_mix,
                    "last_on": _iso(self.fan_last_on),
                    "last_off": _iso(self.fan_last_off),
                },
                "water": {
                    "on": self.water_on,
                    "on_since": _iso(self.water_on_since),
                    "last_hot": _iso(self.water_last_hot),
                    "hygiene_until": _iso(self.water_hygiene_until),
                    "frost_active": self.water_frost_active,
                    "window": self.water_window,
                },
            },
            "readings": {
                "zones": zones,
                "hall_spread": self.hall_temp_spread,
                "ceiling": self._ceiling_temp(),
                "floor": self._floor_temp(stale_min),
                "fan_dt": self.fan_dt,
                "outdoor": self._outdoor_temp(),
                "shared_coldest": self._shared_room_temp(),
                "power": power,
                "fan_o1_w": self._o1_watts(),
                "ceiling_rh": self._ceiling_humidity(),
                "heat_demand": self.heat_demand,
            },
            "events": self.audit.to_list(),
            "trace": self.trace.to_list(),
        }

    async def async_reset_tunables(self) -> None:
        """Restore every tunable helper to its built-in default.

        Called by the "Reset tunables to defaults" button. Resets numbers,
        switches, the boost-duration select and the ECO keyword text, then
        re-evaluates everything that depends on them (seasonal lockout, hall
        setpoints, and one full reconcile). It does not touch boosts, manual
        holds or the latched fan fault — resetting sliders must not silently
        re-arm a faulted fan.
        """
        for registry in (self._numbers, self._switches, self._selects, self._texts):
            for entity in registry.values():
                restore = getattr(entity, "restore_default", None)
                if restore is not None:
                    restore()
        await self._async_seasonal_check()
        await self.async_hall_temps_changed()
        await self.async_reconcile()

    async def async_create_dashboards(self) -> None:
        """Create or refresh the sidebar dashboard (the Create dashboards button).

        Generation itself is pure; only the Lovelace storage calls can fail on
        a Home Assistant version that has reshaped its internals — in which
        case the docs/ YAML files remain the manual fallback and the
        notification says so.
        """
        from . import dashboards
        from .const import NOTIFY_DASHBOARDS

        try:
            error = await dashboards.async_create_or_update(self.hass, self)
        except Exception as err:  # noqa: BLE001 - semi-internal HA API
            error = str(err)
        if error == dashboards.RESTART_REQUIRED:
            persistent_notification.async_create(
                self.hass,
                (
                    "The 'Scout Hut' dashboard has been created and its views "
                    "saved, but this Home Assistant version cannot add it to "
                    "the sidebar live — restart Home Assistant and it will "
                    "appear."
                ),
                title="🏕 Scout Hut – Dashboard created (restart to see it)",
                notification_id=NOTIFY_DASHBOARDS,
            )
        elif error:
            persistent_notification.async_create(
                self.hass,
                (
                    f"Could not create the dashboard automatically ({error}). "
                    "You can still paste docs/heating_dashboard.yaml and "
                    "docs/fan_dashboard.yaml from the repository as manual "
                    "dashboards."
                ),
                title="🏕 Scout Hut – Dashboard creation failed",
                notification_id=NOTIFY_DASHBOARDS,
            )
        else:
            persistent_notification.async_create(
                self.hass,
                (
                    "The 'Scout Hut' dashboard (Heating + Fans views) has been "
                    "created in the sidebar with your real entity ids. Press "
                    "the button again any time to regenerate it — e.g. after "
                    "mapping new hardware."
                ),
                title="🏕 Scout Hut – Dashboard created",
                notification_id=NOTIFY_DASHBOARDS,
            )

    async def async_fan_rearm(self) -> None:
        """Clear an inferred fan fault. This is the deliberate HA-side re-arm.

        Called when the "Ceiling fans enabled" switch is turned on. We never
        auto-rearm inside the loop; a latched fault clears only on this explicit
        gesture (or when a mapped fault boolean clears itself). The physical
        re-arm is still turning the Shelly master on at the device.
        """
        self.fan_fault_latched = False
        self.fan_master_off_since = None
        self._fan_master_seen_unavailable = False
        self._reverse_attempts = 0
        self._fan_fault_notified = False
        persistent_notification.async_dismiss(self.hass, NOTIFY_FAN_FAULT)
        await self.async_reconcile()

    # ------------------------------------------------------------------
    # Desired-state computation
    # ------------------------------------------------------------------
    def _eco_keyword_active(self, zone: str) -> bool:
        title = self.cal_title.get(zone, "")
        return any(kw in title for kw in self.eco_keywords())

    def _cal_active(self, zone: str) -> bool:
        return self._is_on(self.config.get(ZONE_CALENDAR[zone])) or self.cal_window[zone]

    def _reason(self, zone: str, reason: str, preset: str | None) -> str | None:
        """Stash why a zone's desired preset is what it is (for the audit)."""
        self._preset_reason[zone] = reason
        return preset

    def _desired_zone(self, zone: str) -> str | None:
        enabled_key = f"{zone}_automation_enabled"
        if not self.switch_on(enabled_key, default=True):
            return None
        if self.manual_hold[zone]:
            return None
        if self.opening_ice[zone]:
            return self._reason(zone, "opening", PRESET_ICE)
        if self.boost_active(zone):
            return self._reason(zone, "boost", PRESET_COMFORT)
        if self.seasonal_lockout:
            return self._reason(zone, "seasonal_lockout", PRESET_ICE)

        cal_on = self._cal_active(zone)
        alarm_on = self._is_on(self.config.get(ZONE_ALARM[zone]))
        if alarm_on and not cal_on:
            # Arming the alarm with no booking also cancels a lingering
            # occupied override (original A33/A34), or a switch left on weeks
            # ago would silently resume heating the empty zone at disarm.
            override = self._switches.get(f"{zone}_occupied_override")
            force_off = getattr(override, "force_off", None)
            if force_off is not None and override.is_on:
                force_off()
            return self._reason(zone, "alarm", PRESET_ICE)

        timeout = self.number("motion_timeout_minutes")
        area = ZONE_MOTION_AREA[zone]
        if cal_on:
            base = PRESET_ECO if self._eco_keyword_active(zone) else PRESET_COMFORT
            # Drop an unoccupied booking to eco only once the event has
            # actually started. During the pre-heat window the room is empty
            # by definition — demoting there would heat toward eco while the
            # optimum-start lead was sized to reach comfort, so hirers would
            # always arrive to a shortfall.
            event_running = self._is_on(self.config.get(ZONE_CALENDAR[zone]))
            if (
                base == PRESET_COMFORT
                and event_running
                and not self._motion_recent(area, timeout)
            ):
                return self._reason(zone, "booking_quiet", PRESET_ECO)
            if base == PRESET_ECO:
                return self._reason(zone, "booking_eco", base)
            return self._reason(zone, "booking" if event_running else "preheat", base)

        if self.switch_on(f"{zone}_occupied_override"):
            return self._reason(zone, "occupied_override", PRESET_ECO)
        if self._motion_recent(area, timeout):
            return self._reason(zone, "motion", PRESET_ECO)
        if not self._motion_recent_any(timeout):
            return self._reason(zone, "building_empty", PRESET_ICE)
        # Zone quiet but someone is elsewhere in the building: rest at eco rather
        # than leaving a stale comfort preset running (e.g. the hall after a
        # booking ends while a cleaner is still in the kitchen).
        return self._reason(zone, "others_present", PRESET_ECO)

    def _desired_shared(self) -> str | None:
        if not self.config.get(CONF_SHARED_CLIMATES):
            return None
        if self.seasonal_lockout:
            return self._reason("shared", "seasonal_lockout", PRESET_ICE)
        if self.opening_ice["shared"]:
            return self._reason("shared", "opening", PRESET_ICE)
        if self.boost_active(ZONE_A) or self.boost_active(ZONE_B):
            return self._reason("shared", "boost", PRESET_COMFORT)
        if self._is_on(self.config.get(CONF_ALARM_MAIN)) and self._is_on(
            self.config.get(CONF_ALARM_OFFICE)
        ):
            return self._reason("shared", "alarm", PRESET_ICE)
        if self._cal_active(ZONE_A) or self._cal_active(ZONE_B):
            return self._reason("shared", "booking", PRESET_ECO)
        if self._motion_recent_any(self.number("motion_timeout_minutes")):
            return self._reason("shared", "motion", PRESET_ECO)
        return self._reason("shared", "building_empty", PRESET_ICE)

    def _shared_room_temp(self) -> float | None:
        """Coldest reported room temperature around the water heater.

        Reads the shared-zone (kitchen/toilet) Rointe climates, ignoring any
        heater that is unavailable; the tank lives in that zone, so the coldest
        reading is the one that matters for frost.
        """
        vals: list[float] = []
        for climate in self._as_list(self.config.get(CONF_SHARED_CLIMATES)):
            st = self.hass.states.get(climate)
            if st is None or st.state in ("unavailable", "unknown"):
                continue
            temp = st.attributes.get("current_temperature")
            try:
                if temp is not None:
                    vals.append(float(temp))
            except (TypeError, ValueError):
                continue
        return min(vals) if vals else None

    def _water_actual(self) -> bool | None:
        """The real switch state, or None when unknown."""
        st = self.hass.states.get(self.config.get(CONF_WATER_SWITCH))
        if st is not None and st.state in ("on", "off"):
            return st.state == "on"
        return None

    def _desired_water(self) -> bool | None:
        switch = self.config.get(CONF_WATER_SWITCH)
        if not switch:
            return None
        now = self._now()

        # Track the REAL powered stretch (the physical switch, not our last
        # command): a manually flipped or failed switch must not count as
        # heating the tank.
        actual = self._water_actual()
        powered = actual if actual is not None else bool(self.water_on)
        if powered:
            if self.water_on_since is None:
                self.water_on_since = now
        else:
            self.water_on_since = None

        # The stored water only counts as genuinely hot after a continuous
        # powered stretch long enough for a full reheat. A brief dab of power
        # (a short keep-alive, a quick override) raises 15 L by only a few
        # degrees, so it must not reset the weekly hygiene clock — otherwise a
        # week of 5-minute uses would leave the tank permanently lukewarm with
        # the hygiene cycle never firing.
        if self.water_on_since is not None and now - self.water_on_since >= timedelta(
            minutes=WATER_HYGIENE_MINUTES
        ):
            self.water_last_hot = now

        # Frost protection (highest priority, overrides the alarms): the
        # Speedflow's own frost stat only works while powered, so keep it
        # powered whenever the rooms around it are near freezing. Hysteresis so
        # a reading hovering at the trip point cannot flap the switch; a lost
        # reading holds the current state until it returns.
        room = self._shared_room_temp()
        if room is not None:
            if room <= WATER_FROST_ON_TEMP:
                if not self.water_frost_active:
                    self.audit.record("water_frost", now, active=True, room=room)
                self.water_frost_active = True
            elif room >= WATER_FROST_OFF_TEMP:
                if self.water_frost_active:
                    self.audit.record("water_frost", now, active=False, room=room)
                self.water_frost_active = False
        if self.water_frost_active:
            return True

        # Weekly hygiene heat-up (also overrides the alarms): if the tank has
        # gone a week without a completed reheat, run it long enough for the
        # full 15 L to reach thermostat temperature, so stored water never sits
        # lukewarm indefinitely between lets.
        if self.water_hygiene_until is not None:
            if now < self.water_hygiene_until:
                return True
            # Window over: if the switch really was on at the end, that is a
            # completed reheat — credit it directly so the cycle cannot
            # immediately re-trigger itself.
            self.audit.record(
                "water_hygiene", now, phase="complete" if powered else "interrupted"
            )
            if powered:
                self.water_last_hot = now
            self.water_hygiene_until = None
        if self.water_last_hot is None:
            self.water_last_hot = now  # start the clock on first evaluation
        elif now - self.water_last_hot >= WATER_HYGIENE_INTERVAL:
            self.water_hygiene_until = now + timedelta(minutes=WATER_HYGIENE_MINUTES)
            self.audit.record("water_hygiene", now, phase="start")
            return True

        override = self.switch_on("water_manual_override")
        cal = self.water_window
        keepalive = self.number("water_motion_keepalive_minutes")
        motion = any(self._motion_recent(a, keepalive) for a in WATER_MOTION_AREAS)
        both_alarms = self._is_on(self.config.get(CONF_ALARM_MAIN)) and self._is_on(
            self.config.get(CONF_ALARM_OFFICE)
        )
        if both_alarms:
            return override or cal
        return override or cal or motion

    # ------------------------------------------------------------------
    # The reconcile loop
    # ------------------------------------------------------------------
    async def async_reconcile(self) -> None:
        """Recompute and apply desired state for every zone."""
        if not self._started:
            return
        if self._reconciling:
            self._reconcile_pending = True
            return
        self._reconciling = True
        try:
            self._refresh_motion_from_states()
            await self._evaluate_openings()
            self._record_booking_edges()
            await self._reconcile_zones()
            self._update_warmup_learning()
            self._update_cooloff_learning()
            await self._reconcile_shared()
            await self._reconcile_water()
            await self._reconcile_fans()
            self._check_condensation()
            self._sample_trace()
            self._detect_drift()
            self._expire_boosts()
            self._store.async_delay_save(self._state_snapshot, 60)
            async_dispatcher_send(self.hass, SIGNAL_UPDATE)
        finally:
            self._reconciling = False
        if self._reconcile_pending:
            self._reconcile_pending = False
            await self.async_reconcile()

    def _sample_trace(self) -> None:
        """Append a point to the rolling temperature/wattage trace.

        Runs every reconcile; the Trace itself throttles to one point per
        15 minutes. These are the exact computed values the decisions used
        (the hall average IS the fan logic's floor reading), which exist as
        no single Home Assistant entity — so the diagnostics download can
        show the curves the audited decisions were reacting to.
        """
        self.trace.maybe_sample(
            self._now(),
            ceiling=self._ceiling_temp(),
            floor=self._floor_temp(),
            hall_coldest=self._zone_room_temp(ZONE_A, coldest=True),
            office=self._zone_room_temp(ZONE_B),
            shared=self._shared_room_temp(),
            outdoor=self._outdoor_temp(),
            rh=self._ceiling_humidity(),
            o1_w=self._o1_watts(),
            fans=bool(self.fan_on),
            demand=self.heat_demand,
        )

    def _record_booking_edges(self) -> None:
        """Audit the moment each booking begins and ends.

        The start temperature against the target is the ground truth for
        whether the optimum-start lead was sized right — the one number that
        judges the whole learning stack. A positive shortfall means the
        coldest end arrived under target; negative means it was already
        warmer (the lead, or the seed, is oversized). The end event marks
        when the CONTROLLER saw the calendar entity finish — so a fan or
        preset change shortly after can be read against it — and its
        temperature anchors the cool-off that follows.
        """
        for zone in (ZONE_A, ZONE_B):
            cal = self.config.get(ZONE_CALENDAR[zone])
            if not cal:
                continue
            running = self._is_on(cal)
            was = self._cal_running_prev[zone]
            self._cal_running_prev[zone] = running
            if was is None or running == was:
                continue
            if not running:
                self.audit.record(
                    "booking_end",
                    self._now(),
                    zone=zone,
                    coldest=self._zone_room_temp(zone, coldest=True),
                    average=self._zone_room_temp(zone),
                    outdoor=self._outdoor_temp(),
                    preset=self.applied[zone],
                )
                continue
            eco = self._eco_keyword_active(zone)
            target = self.number("hall_eco_low_temp") if eco else self._zone_target(zone)
            coldest = self._zone_room_temp(zone, coldest=True)
            self.audit.record(
                "booking_start",
                self._now(),
                zone=zone,
                title=self.cal_title.get(zone) or None,
                eco=eco,
                target=target,
                coldest=coldest,
                average=self._zone_room_temp(zone),
                shortfall=None if coldest is None else target - coldest,
                outdoor=self._outdoor_temp(),
                preset=self.applied[zone],
            )

    def _refresh_motion_from_states(self) -> None:
        """Refresh timestamps for any motion sensor currently reading 'on'."""
        now = self._now()
        for area, key in MOTION_AREAS.items():
            if self._is_on(self.config.get(key)):
                self.last_motion[area] = now

    async def _evaluate_openings(self) -> None:
        door_mins = self.number("door_ice_minutes")
        window_mins = self.number("window_ice_minutes")
        internal_open = self._is_on(self.config.get(CONF_INTERNAL_DOOR))
        through_path = internal_open and (
            self._exterior_open(ZONE_A) or self._exterior_open(ZONE_B)
        )

        for zone in (ZONE_A, ZONE_B):
            doors = self._as_list(self.config.get(ZONE_DOORS[zone]))
            windows = self._as_list(self.config.get(ZONE_WINDOWS[zone]))
            held = self._open_held(f"{zone}_doors", doors, door_mins) or self._open_held(
                f"{zone}_windows", windows, window_mins
            )
            should_ice = held or through_path
            was = self.opening_ice[zone]
            self.opening_ice[zone] = should_ice
            if should_ice and not was:
                self.manual_hold[zone] = False
                if through_path and not held:
                    persistent_notification.async_create(
                        self.hass,
                        "The internal door is open together with an exterior "
                        "door or window, creating a heat-loss path. Heating is "
                        "paused until they are closed.",
                        title="🏕 Scout Hut – Internal door + exterior opening",
                        notification_id=NOTIFY_INTERNAL_DOOR,
                    )
                else:
                    persistent_notification.async_create(
                        self.hass,
                        "A door or window has been held open. Heating is paused "
                        "and will restore when everything is closed.",
                        title=f"🏕 {zone.replace('_', ' ').title()} – Heating paused",
                        notification_id=NOTIFY_ZONE_OPENING[zone],
                    )
            elif was and not should_ice:
                persistent_notification.async_dismiss(self.hass, NOTIFY_ZONE_OPENING[zone])
                persistent_notification.async_dismiss(self.hass, NOTIFY_INTERNAL_DOOR)

        shared_windows = self._as_list(self.config.get(CONF_SHARED_WINDOWS))
        shared_held = self._open_held("shared_windows", shared_windows, window_mins)
        was_shared = self.opening_ice["shared"]
        self.opening_ice["shared"] = shared_held
        if shared_held and not was_shared:
            persistent_notification.async_create(
                self.hass,
                "A toilet or kitchen window has been held open. Shared-zone "
                "heating is paused until it is closed.",
                title="🏕 Shared zone – Heating paused",
                notification_id=NOTIFY_SHARED_OPENING,
            )
        elif was_shared and not shared_held:
            persistent_notification.async_dismiss(self.hass, NOTIFY_SHARED_OPENING)

    async def _reconcile_zones(self) -> None:
        for zone in (ZONE_A, ZONE_B):
            desired = self._desired_zone(zone)
            if desired is None:
                continue
            # If the last apply went out while a heater was offline, re-send once
            # every heater in the zone is back online, even if the target is
            # unchanged (the offline heater may never have received it).
            if self._zone_offline_apply.get(zone) and self._all_zone_online(zone):
                await self._async_set_preset(zone, desired)
                continue
            if desired == self.applied[zone]:
                self.expected_preset[zone] = desired
                continue
            await self._async_set_preset(zone, desired)

    async def _reconcile_shared(self) -> None:
        desired = self._desired_shared()
        if desired is None:
            return
        climates = self._as_list(self.config.get(CONF_SHARED_CLIMATES))
        all_online = bool(climates) and all(self._climate_online(c) for c in climates)
        # Mirror the zones' offline handling: a preset sent while a shared
        # heater was unreachable is re-sent once every heater is back, so the
        # kitchen/toilet radiators (the frost-critical room) cannot sit on a
        # wrong preset indefinitely after a cloud blip. A *changed* desired is
        # always sent immediately, exactly like the zones.
        if desired == self.applied["shared"]:
            if not (self._shared_offline_apply and all_online):
                return  # nothing to do, or still waiting for reconnection
        else:
            self.audit.record(
                "preset",
                self._now(),
                zone="shared",
                previous=self.applied["shared"],
                to=desired,
                reason=self._preset_reason.get("shared"),
            )
        await self._async_apply_climate(climates, desired)
        self.applied["shared"] = desired
        self._shared_offline_apply = not all_online

    async def _reconcile_water(self) -> None:
        desired = self._desired_water()
        if desired is None:
            return
        # Reconcile against the real switch state, not the last command, so an
        # external flip (HA UI, a Shelly reboot losing relay state, a failed
        # call) is re-asserted on the next tick — frost protection must not be
        # defeatable by one manual toggle.
        actual = self._water_actual()
        current = actual if actual is not None else self.water_on
        if desired == current:
            self.water_on = desired
            return
        await self.hass.services.async_call(
            "switch",
            "turn_on" if desired else "turn_off",
            {"entity_id": self.config.get(CONF_WATER_SWITCH)},
            blocking=False,
        )
        self.water_on = desired
        # Power starts/stops with the command we just sent; if it did not
        # actually take, the next tick's actual-state check corrects this
        # before any reheat credit (45 min) could accrue.
        if desired:
            if self.water_on_since is None:
                self.water_on_since = self._now()
        else:
            self.water_on_since = None

    def _expire_boosts(self) -> None:
        now = self._now()
        for zone in (ZONE_A, ZONE_B):
            until = self.boost_until.get(zone)
            if until is not None and now >= until:
                self.boost_until[zone] = None
                self._reconcile_pending = True

    def _detect_drift(self) -> None:
        """Flag a manual hold when a zone's preset differs from what we set."""
        for zone in (ZONE_A, ZONE_B):
            if not self.switch_on(f"{zone}_automation_enabled", default=True):
                continue
            if not self._cal_active(zone):
                # The hold is documented to last "until the booking ends" —
                # release it once the booking is over, or an app change made
                # mid-booking would freeze the zone's automation indefinitely.
                if self.manual_hold[zone]:
                    self.manual_hold[zone] = False
                    persistent_notification.async_dismiss(self.hass, NOTIFY_ZONE_HOLD[zone])
                continue
            if self.boost_active(zone):
                continue
            expected = self.expected_preset[zone]
            if expected is None:
                continue
            # Ignore drift within the settle window of our own change — the
            # Rointe cloud can take a couple of minutes to reflect it.
            last = self._last_apply.get(zone)
            if last is not None and (self._now() - last).total_seconds() < DRIFT_SETTLE_SECONDS:
                continue
            climates = self._as_list(self.config.get(ZONE_CLIMATES[zone]))
            if not climates:
                continue
            # Do not read drift from an offline heater: a stale preset would look
            # like a manual change. Resume once it is back and readable.
            if not self._climate_online(climates[0]):
                continue
            state = self.hass.states.get(climates[0])
            if state is None:
                continue
            actual = (state.attributes.get("preset_mode") or "").lower()
            if actual:
                matches: bool | None = actual == expected
                detail = f"heater is {actual}"
            else:
                # The Rointe integration in the field accepts set_preset_mode
                # but reports preset_mode as null, so judge drift from the
                # reported SETPOINT instead: each preset implies a known
                # target temperature on a Rointe (anti-frost is fixed at 7).
                matches = self._setpoint_matches(zone, state, expected)
                detail = f"target is {state.attributes.get('temperature')}°C"
            if matches is None:
                continue
            if not matches and not self.manual_hold[zone]:
                self.manual_hold[zone] = True
                self.audit.record(
                    "manual_hold", self._now(), zone=zone, expected=expected, seen=detail
                )
                persistent_notification.async_create(
                    self.hass,
                    f"Heating was changed manually (expected {expected}, "
                    f"{detail}). Automation is paused until the booking ends.",
                    title=f"🏕 {zone.replace('_', ' ').title()} – Manual control detected",
                    notification_id=NOTIFY_ZONE_HOLD[zone],
                )
            elif matches and self.manual_hold[zone]:
                self.manual_hold[zone] = False
                persistent_notification.async_dismiss(self.hass, NOTIFY_ZONE_HOLD[zone])

    def _setpoint_matches(self, zone: str, state: Any, expected: str) -> bool | None:
        """Does the heater's reported setpoint agree with the expected preset?

        Returns None when it cannot be judged (no readable setpoint, or the
        preset's implied temperature is unknown for this zone) — the caller
        skips rather than guesses.
        """
        try:
            setpoint = float(state.attributes.get("temperature"))
        except (TypeError, ValueError):
            return None
        tol = SETPOINT_TOLERANCE
        if expected == PRESET_ICE:
            return abs(setpoint - ROINTE_ANTIFROST) <= tol
        if expected == PRESET_COMFORT:
            if zone == ZONE_A:
                return abs(setpoint - self.number("hall_comfort_temp")) <= tol
            cached = self._zone_comfort_target.get(zone)
            return None if cached is None else abs(setpoint - cached) <= tol
        if expected == PRESET_ECO:
            if zone == ZONE_A:
                # Either eco value we push is legitimate (eco-low for ECO
                # keyword events, plain eco otherwise).
                eco = self.number("hall_eco_temp")
                eco_low = self.number("hall_eco_low_temp")
                return min(abs(setpoint - eco), abs(setpoint - eco_low)) <= tol
            # The office eco setpoint lives on the device and is never
            # pushed by the integration, so it cannot be judged.
            return None
        return None

    # ------------------------------------------------------------------
    # Applying presets
    # ------------------------------------------------------------------
    async def _async_set_preset(self, zone: str, preset: str, force: bool = False) -> None:
        climates = self._as_list(self.config.get(ZONE_CLIMATES[zone]))
        if not climates:
            return
        if preset != self.applied[zone]:
            self.audit.record(
                "preset",
                self._now(),
                zone=zone,
                previous=self.applied[zone],
                to=preset,
                reason=self._preset_reason.get(zone),
            )
        if zone == ZONE_A and preset in (PRESET_COMFORT, PRESET_ECO) and not force:
            await self._async_push_hall_temps(eco_low=self._eco_keyword_active(zone))
        await self._async_apply_climate(climates, preset)
        self.applied[zone] = preset
        self.expected_preset[zone] = preset
        self._last_apply[zone] = self._now()
        # Remember if any heater was offline: the command cannot have reached it,
        # so mark the zone for a re-send once it reconnects.
        self._zone_offline_apply[zone] = not self._all_zone_online(zone)

    def _hall_number_entities(self) -> tuple[list[str], list[str]]:
        """Resolve the hall comfort / eco temperature number entities.

        Uses whatever the user mapped explicitly; for either side left blank it
        auto-discovers the matching ``number`` entities from the same device as
        each mapped hall climate entity (Rointe exposes a comfort and an eco
        temperature number per heater). Eco-low is not a separate entity — it is
        just a lower value written to the eco number, so there is nothing to map
        for it.
        """
        comfort = self._as_list(self.config.get(CONF_HALL_COMFORT_NUMBERS))
        eco = self._as_list(self.config.get(CONF_HALL_ECO_NUMBERS))
        if comfort and eco:
            return comfort, eco
        auto_comfort, auto_eco = self._discover_hall_numbers()
        return comfort or auto_comfort, eco or auto_eco

    def _discover_hall_numbers(self) -> tuple[list[str], list[str]]:
        """Find comfort/eco temperature numbers on the hall heaters' devices."""
        registry = er.async_get(self.hass)
        comfort: list[str] = []
        eco: list[str] = []
        for climate in self._as_list(self.config.get(CONF_HALL_CLIMATES)):
            entry = registry.async_get(climate)
            if entry is None or entry.device_id is None:
                continue
            for member in er.async_entries_for_device(
                registry, entry.device_id, include_disabled_entities=False
            ):
                if member.domain != "number":
                    continue
                eid = member.entity_id.lower()
                if "comfort" in eid:
                    comfort.append(member.entity_id)
                elif "eco" in eid:
                    eco.append(member.entity_id)
        return comfort, eco

    async def _async_push_hall_temps(self, eco_low: bool) -> None:
        comfort_numbers, eco_numbers = self._hall_number_entities()
        comfort_temp = self.number("hall_comfort_temp")
        eco_temp = self.number("hall_eco_low_temp") if eco_low else self.number("hall_eco_temp")
        if comfort_numbers:
            await self.hass.services.async_call(
                "number",
                "set_value",
                {"entity_id": comfort_numbers, "value": comfort_temp},
                blocking=False,
            )
        if eco_numbers:
            await self.hass.services.async_call(
                "number",
                "set_value",
                {"entity_id": eco_numbers, "value": eco_temp},
                blocking=False,
            )

    async def _async_apply_climate(self, entities: Any, preset: str) -> None:
        climates = self._as_list(entities)
        if not climates:
            return
        await self.hass.services.async_call(
            "climate",
            "set_preset_mode",
            {"entity_id": climates, "preset_mode": preset},
            blocking=False,
        )

    # ------------------------------------------------------------------
    # Destratification / cooling fans
    #
    # Home Assistant only decides when the fans are wanted and in which
    # direction. The Shelly Pro 2PM script owns all timing and safety (the 45 s
    # coast-down dwell, the coil verification, stall / low-tap protection and the
    # latched fault). We never reproduce any of that here.
    #
    # Direction relay (O2 / switch.fan_direction): OFF/open = forward (down air,
    # summer cooling); ON/closed = reverse (up air, winter destratification).
    # A live direction change always goes through the reverse button.
    # ------------------------------------------------------------------
    def _num_state(self, entity_id: str | None) -> float | None:
        """Return a numeric state, or None if missing / non-numeric."""
        if not entity_id:
            return None
        st = self.hass.states.get(entity_id)
        if st is None or st.state in ("unknown", "unavailable", None, ""):
            return None
        try:
            return float(st.state)
        except (TypeError, ValueError):
            return None

    def _stale(self, entity_id: str | None, stale_min: float) -> bool:
        """Return True if the sensor is missing or has not reported recently.

        Freshness is judged from ``last_reported`` when available (it advances on
        every report, even when the value is unchanged) and falls back to
        ``last_updated``. This matters for a Shelly H&T that sits at a steady
        temperature: its value does not change but it keeps reporting, so it must
        not be treated as stale.
        """
        if not entity_id:
            return False
        st = self.hass.states.get(entity_id)
        if st is None or st.state in ("unknown", "unavailable"):
            return True
        ts = getattr(st, "last_reported", None) or st.last_updated
        return (dt_util.utcnow() - ts).total_seconds() > stale_min * 60

    def _ceiling_temp(self) -> float | None:
        return self._num_state(self.config.get(CONF_CEILING_TEMP))

    def _ceiling_humidity(self) -> float | None:
        """RH from the ceiling H&T's own humidity sensor (auto-discovered)."""
        if not self._humidity_entity:
            found = self._discover_ceiling_humidity()
            if found:
                self._humidity_entity = found
            return self._num_state(found) if found else None
        return self._num_state(self._humidity_entity)

    def _discover_ceiling_humidity(self) -> str | None:
        """Find the humidity sensor on the ceiling sensor's device."""
        ceiling = self.config.get(CONF_CEILING_TEMP)
        if not ceiling:
            return None
        registry = er.async_get(self.hass)
        entry = registry.async_get(ceiling)
        if entry is None or entry.device_id is None:
            return None
        for member in er.async_entries_for_device(
            registry, entry.device_id, include_disabled_entities=False
        ):
            if member.domain != "sensor":
                continue
            if (getattr(member, "original_device_class", None) or "") == "humidity" or (
                "humidity" in member.entity_id.lower()
            ):
                return member.entity_id
        return None

    def _check_condensation(self) -> None:
        """Winter fabric watch: notify on a sustained cold-and-damp hall.

        Historic England recommends 8-10 °C background for unoccupied fabric;
        the Rointe anti-frost floor is fixed at 7 °C, so the gap is covered by
        watching for the failure signature instead: high humidity held for
        many hours while the hall fabric is cold. Only meaningful in the
        heating season — a warm summer hall does not condense on its walls.
        """
        rh = self._ceiling_humidity()
        floor = self._floor_temp()
        now = self._now()
        threshold = CONDENSATION_RH_OFF if self._rh_high_since else CONDENSATION_RH_ON
        cold_damp = (
            not self.seasonal_lockout
            and rh is not None
            and floor is not None
            and floor <= CONDENSATION_MAX_TEMP
            and rh >= threshold
        )
        if cold_damp:
            if self._rh_high_since is None:
                self._rh_high_since = now
            elif (
                not self._condensation_notified
                and now - self._rh_high_since >= timedelta(hours=CONDENSATION_HOURS)
            ):
                self._condensation_notified = True
                self.audit.record("condensation", now, rh=rh, floor=floor)
                persistent_notification.async_create(
                    self.hass,
                    (
                        f"The hall has sat at {rh:.0f}% humidity below "
                        f"{CONDENSATION_MAX_TEMP:.0f}°C for over "
                        f"{CONDENSATION_HOURS:.0f} hours — conditions where "
                        "moisture condenses on cold fabric and mould follows. "
                        "Consider a spell of background heat (Boost) or airing "
                        "the building on the next dry day."
                    ),
                    title="🏕 Scout Hut – Cold and damp: condensation risk",
                    notification_id=NOTIFY_CONDENSATION,
                )
            return
        self._rh_high_since = None
        if self._condensation_notified:
            self._condensation_notified = False
            persistent_notification.async_dismiss(self.hass, NOTIFY_CONDENSATION)

    def _floor_temp(self, stale_min: float | None = None) -> float | None:
        """Floor / occupant temperature.

        Uses an explicit floor sensor if mapped, otherwise the average
        ``current_temperature`` of the hall heaters (the Rointe climates report
        the room temperature at floor level). Because the Rointe integration is
        cloud based, a heater that is offline (``unavailable``) or has stopped
        updating (frozen cloud) is dropped from the average, so a stale reading is
        never trusted; if that leaves nothing, this returns None and the caller
        treats the floor as lost.
        """
        override = self.config.get(CONF_FLOOR_TEMP)
        if override:
            # A frozen reading must not be trusted here either: a stale floor
            # sensor otherwise keeps driving the summer warm/overheated
            # decisions long after the ΔT logic has flagged it lost.
            if stale_min is not None and self._stale(override, stale_min):
                return None
            return self._num_state(override)
        vals: list[float] = []
        for climate in self._as_list(self.config.get(CONF_HALL_CLIMATES)):
            st = self.hass.states.get(climate)
            if st is None or st.state in ("unavailable", "unknown"):
                continue
            if stale_min is not None:
                ts = getattr(st, "last_reported", None) or st.last_updated
                if (dt_util.utcnow() - ts).total_seconds() > stale_min * 60:
                    continue
            temp = st.attributes.get("current_temperature")
            try:
                if temp is not None:
                    vals.append(float(temp))
            except (TypeError, ValueError):
                continue
        return sum(vals) / len(vals) if vals else None

    def _heat_demand(self) -> bool:
        """True if any Rointe heater is actively producing heat right now.

        Reads the Rointe Effective Power sensors: any above the tunable watt
        threshold means that heater is calling. This catches office (or shared)
        heaters warming the poorly-insulated hall, not just hall demand. The
        sensors are auto-detected from the mapped heater devices, so nothing extra
        needs mapping; an explicit mapping overrides the auto-detection. If none
        can be read it falls back to whether any hall/office zone is on a heating
        preset. A power sensor that is offline or has stopped updating (frozen
        cloud) is ignored rather than trusted at its last value.
        """
        threshold = self.number("heat_demand_watts")
        stale_min = self.number("fan_sensor_stale_minutes")
        seen_value = False
        for power in self._power_sensors():
            value = self._num_state(power)
            if value is None:
                continue
            # Any readable value proves the sensors exist — a summer's worth of
            # unchanging 0 W must NOT fall through to the preset fallback,
            # which would call an idle eco preset "demand". Freshness gates
            # only the positive trigger, so a frozen high reading cannot
            # assert demand either.
            seen_value = True
            if value > threshold and not self._stale(power, stale_min):
                return True
        if seen_value:
            return False
        return any(self.applied[z] in (PRESET_COMFORT, PRESET_ECO) for z in (ZONE_A, ZONE_B))

    def _power_sensors(self) -> list[str]:
        """Resolve the Rointe Effective Power sensors.

        Uses an explicit mapping if given; otherwise auto-detects them from the
        heater devices (mirrors how the hall comfort/eco numbers are found) and
        memoises the first non-empty result.
        """
        mapped = self._as_list(self.config.get(CONF_ROINTE_POWER))
        if mapped:
            return mapped
        if self._discovered_power is None:
            found = self._discover_power_sensors()
            if found:
                self._discovered_power = found
            return found
        return self._discovered_power

    def _discover_power_sensors(self) -> list[str]:
        """Find the Effective Power sensor on each mapped heater's device.

        Looks across the hall, office and shared heaters (any of their power can
        signal that heat is being produced in the building) for a sibling sensor
        with a power device class, or an ``*_power`` entity id that is not the
        energy total.
        """
        registry = er.async_get(self.hass)
        found: list[str] = []
        climates = (
            self._as_list(self.config.get(CONF_HALL_CLIMATES))
            + self._as_list(self.config.get(CONF_OFFICE_CLIMATES))
            + self._as_list(self.config.get(CONF_SHARED_CLIMATES))
        )
        for climate in climates:
            entry = registry.async_get(climate)
            if entry is None or entry.device_id is None:
                continue
            device_matches: list[str] = []
            for member in er.async_entries_for_device(
                registry, entry.device_id, include_disabled_entities=False
            ):
                if member.domain != "sensor":
                    continue
                eid = member.entity_id.lower()
                is_power = (getattr(member, "original_device_class", None) or "") == "power" or (
                    "power" in eid and "energy" not in eid
                )
                if is_power:
                    device_matches.append(member.entity_id)
            # Rointe devices expose both a constant NOMINAL "power" (the
            # radiator's rating, always fresh, always above the demand
            # threshold) and the live "effective power". Only the latter says
            # anything about demand — prefer it whenever it exists.
            effective = [e for e in device_matches if "effective" in e.lower()]
            found.extend(effective or device_matches)
        return found

    def _connected_for(self, climate: str) -> str | None:
        """Return the Rointe 'Connected' binary_sensor for a heater, if any."""
        if self._connected_map is None:
            found = self._discover_connected_map()
            if found:
                self._connected_map = found
            return found.get(climate)
        return self._connected_map.get(climate)

    def _discover_connected_map(self) -> dict[str, str]:
        """Map each mapped heater climate to its 'Connected' binary_sensor."""
        registry = er.async_get(self.hass)
        mapping: dict[str, str] = {}
        climates = (
            self._as_list(self.config.get(CONF_HALL_CLIMATES))
            + self._as_list(self.config.get(CONF_OFFICE_CLIMATES))
            + self._as_list(self.config.get(CONF_SHARED_CLIMATES))
        )
        for climate in climates:
            entry = registry.async_get(climate)
            if entry is None or entry.device_id is None:
                continue
            for member in er.async_entries_for_device(
                registry, entry.device_id, include_disabled_entities=False
            ):
                if member.domain != "binary_sensor":
                    continue
                if (member.original_device_class or "") == "connectivity" or (
                    "connect" in member.entity_id.lower()
                ):
                    mapping[climate] = member.entity_id
                    break
        return mapping

    def _climate_online(self, climate: str) -> bool:
        """Whether a heater is reachable.

        Prefers its Rointe 'Connected' sensor; falls back to the climate entity
        not being unavailable / unknown when no connectivity sensor is found.
        """
        connected = self._connected_for(climate)
        if connected:
            st = self.hass.states.get(connected)
            if st is not None and st.state not in ("unknown", "unavailable"):
                return st.state == "on"
        st = self.hass.states.get(climate)
        return st is not None and st.state not in ("unavailable", "unknown")

    def _all_zone_online(self, zone: str) -> bool:
        climates = self._as_list(self.config.get(ZONE_CLIMATES[zone]))
        return bool(climates) and all(self._climate_online(c) for c in climates)

    def _mapped_fault(self) -> bool:
        """The Shelly-published fault boolean, when mapped and readable."""
        mapped = self.config.get(CONF_FAN_FAULT)
        if mapped:
            st = self.hass.states.get(mapped)
            if st is not None and st.state not in ("unknown", "unavailable"):
                return st.state == "on"
        return False

    @property
    def fan_fault_effective(self) -> bool:
        """Read-only fault state for diagnostics (never mutates the latch).

        The Shelly-published boolean (script-detected faults) and the HA-side
        inferred latch (an unexpected master-off) are independent fault
        sources, so either one holds the fault: the Shelly clearing its own
        boolean must not silently discard an inferred latch, which only the
        deliberate re-arm clears.
        """
        return self._mapped_fault() or self.fan_fault_latched

    def _fan_fault(self) -> bool:
        """Evaluate (and, for the inferred case, latch) the fan fault.

        The inferred fault fires when the master reads off while we expected it
        on for longer than the reverse dwell — but never during the Shelly's
        own reversal grace, and never while the master entity is unavailable
        (an unpowered/rebooting Shelly is not a manual kill). A master that
        comes back READABLE-OFF after having been unavailable is a device
        reboot (wall switch, power cut) with outputs defaulting off: the
        expectation is reset so the reconciler simply re-establishes the wanted
        state on this same tick, instead of latching or deadlocking. The
        inferred latch never auto-rearms; it clears only via async_fan_rearm.
        """
        now = self._now()
        in_grace = (
            self.fan_action_grace_until is not None
            and now < self.fan_action_grace_until
        )
        master = self.config.get(CONF_FAN_MASTER)
        master_st = self.hass.states.get(master) if master else None
        master_known = master_st is not None and master_st.state not in (
            "unknown",
            "unavailable",
        )
        if not master_known:
            if master:
                self._fan_master_seen_unavailable = True
            self.fan_master_off_since = None
            return self._mapped_fault() or self.fan_fault_latched
        if in_grace or not self.fan_master_expected:
            self.fan_master_off_since = None
            return self._mapped_fault() or self.fan_fault_latched
        if master_st.state == "on":
            self.fan_master_off_since = None
            self._fan_master_seen_unavailable = False
        elif self._fan_master_seen_unavailable:
            # Clean reboot recovery: forget the stale expectation so the
            # actuator re-commands from scratch (direction preset while off,
            # then master on) within this reconcile.
            self._fan_master_seen_unavailable = False
            self.fan_master_expected = False
            self.fan_master_off_since = None
        else:
            if self.fan_master_off_since is None:
                self.fan_master_off_since = now
            elif (now - self.fan_master_off_since).total_seconds() >= FAN_FAULT_GRACE:
                if not self.fan_fault_latched:
                    self.audit.record("fan_fault", now, reason="master_off")
                self.fan_fault_latched = True
        return self._mapped_fault() or self.fan_fault_latched

    def _cooling_occupied(self) -> bool:
        """Whether anyone is there for the summer breeze to cool.

        Recent hall motion, or a hall calendar event actually RUNNING (kept so
        a seated group outside PIR coverage doesn't lose its breeze). The
        pre-heat window deliberately does NOT count: a fan cannot pre-cool a
        room — its benefit is instantaneous wind-chill on the people under it,
        so running early would only add motor heat to an empty hall.
        """
        timeout = self.number("motion_timeout_minutes")
        return self._motion_recent("hall", timeout) or self._is_on(
            self.config.get(ZONE_CALENDAR[ZONE_A])
        )

    def _summer_active(self) -> bool:
        """Whether the fans should run the summer cooling regime.

        The manual "Summer cooling mode" switch forces it on; otherwise, with
        "Summer cooling follows season" enabled (the default), the regime
        tracks the seasonal heating lockout — cooling while heating is locked
        out for the season, winter destratification once it releases. Nobody
        has to remember the changeover, and reversals stay seasonal-rare.
        """
        if self.switch_on("summer_mode", default=False):
            return True
        return self.switch_on("summer_follows_season", default=True) and self.seasonal_lockout

    def _fan_target(self) -> tuple[bool, str | None, str]:
        """Resolve the desired fan state with fail-safe precedence on top."""
        if not self.switch_on("fans_enabled", default=True):
            self.fan_sensor_stale = False
            self.fan_dt = None
            self.fan_overheated = False
            self.fan_breeze_hot = False
            self._breeze_latch = False
            self.fan_mix = None
            self._fan_occupied = None
            self._fan_warm = None
            return False, None, "off"
        if self._fan_fault():
            # Reset the condition flags like the disabled branch does, so
            # stale pre-fault values cannot keep feeding fail_safe_off or the
            # diagnostics while the latch holds.
            self.fan_sensor_stale = False
            self.fan_dt = None
            self.fan_overheated = False
            self.fan_breeze_hot = False
            self._breeze_latch = False
            self.fan_mix = None
            self._fan_occupied = None
            self._fan_warm = None
            return False, None, "off"

        stale_min = self.number("fan_sensor_stale_minutes")
        floor_id = self.config.get(CONF_FLOOR_TEMP)
        ct = self._ceiling_temp()
        ft = self._floor_temp(stale_min)
        # The ceiling H&T is a LOCAL threshold reporter (it only transmits on a
        # 0.5 °C change, with no periodic-report setting), so a long silence
        # means "unchanged", not "lost" — its freshness is judged from entity
        # availability alone (the Shelly integration marks a dead device
        # unavailable). The last_reported staleness check stays for the floor /
        # Rointe readings, where a cloud integration can freeze while looking
        # alive.
        ceiling_bad = ct is None
        floor_bad = ft is None or (bool(floor_id) and self._stale(floor_id, stale_min))
        sensors_bad = ceiling_bad or floor_bad
        self.fan_sensor_stale = sensors_bad
        if floor_bad:
            # A lost floor reading must not feed warm/overheated/recirc below:
            # fan_decision's contract is warm=None when the floor is unknown.
            ft = None

        if sensors_bad:
            self.fan_dt = None
            dt: float | None = None
        else:
            dt = ct - ft
            self.fan_dt = dt

        warm = None if ft is None else ft > self.number("cooling_temp_high")
        overheated = ft is not None and ft >= FAN_COOLING_MAX_TEMP
        self.fan_overheated = overheated
        self._fan_warm = warm

        # Hot-breeze guard: once the MIXED air the fans would fold down to
        # head height (~0.75 x floor + 0.25 x ceiling) reaches the tunable
        # ceiling, a breeze gives diminishing-to-negative benefit — hold the
        # summer fans and tell people to open the doors instead. Releases 1 °C
        # below the threshold so a value hovering there cannot flap the fans.
        if ct is not None and ft is not None:
            self.fan_mix = 0.75 * ft + 0.25 * ct
            max_mix = self.number("cooling_mix_max_temp")
            if self.fan_mix >= max_mix:
                self._breeze_latch = True
            elif self.fan_mix <= max_mix - 1.0:
                self._breeze_latch = False
        else:
            self.fan_mix = None

        # Ventilation override, effect-verified: ANY open mapped contact
        # (either zone, shared, internal — all can feed a cross-draft) grants
        # the fans a provisional pass, kept while the venting at least HOLDS
        # the line. The anchor ratchets down to the best (lowest) mix seen
        # since venting began; only a genuine climb above it — the measured
        # signature of solar charge winning (~1.8 °C/h with nothing open) —
        # revokes the pass. Slow-but-real venting against a small
        # indoor-outdoor gap therefore keeps its fans; a token window that
        # changes nothing while the hall keeps heating hands the hold back.
        vent = self._any_opening_open()
        if self._breeze_latch and vent:
            if self._vent_effective and self.fan_mix is not None:
                if self._vent_anchor_mix is None or self.fan_mix < self._vent_anchor_mix:
                    self._vent_anchor_mix = self.fan_mix  # ratchet down only
                elif self.fan_mix >= self._vent_anchor_mix + BREEZE_VENT_MAX_RISE:
                    self._vent_effective = False
        else:
            self._vent_anchor_mix = None
            self._vent_effective = True
        self.fan_breeze_hot = self._breeze_latch and not (vent and self._vent_effective)
        # Recirculate residual / leaked ceiling heat while the occupied zone is
        # still below the cap, decoupled from whether a heater is drawing power.
        recirc_ok = ft is not None and ft < self.number("fan_recirc_max_floor_temp")
        occupied = self._cooling_occupied()
        self._fan_occupied = occupied
        demand = self._heat_demand()
        self.heat_demand = demand
        currently_winter = bool(self.fan_on) and self.fan_mode == "winter"

        return fan_decision(
            summer=self._summer_active(),
            occupied=occupied,
            warm=warm,
            # The breeze guard holds the summer fans exactly like the hard
            # overheat cutoff does (the flag only gates the summer branch);
            # the notifications stay distinct.
            overheated=overheated or self.fan_breeze_hot,
            dt=dt,
            dt_on=self.number("fan_dt_on"),
            dt_off=self.number("fan_dt_off"),
            demand=demand,
            recirc_ok=recirc_ok,
            currently_winter=currently_winter,
            run_on_loss=self.switch_on("fans_run_on_sensor_loss", default=True),
        )

    async def _reconcile_fans(self) -> None:
        """Apply the desired fan state, honouring the anti-short-cycle timers."""
        if not self.config.get(CONF_FAN_MASTER):
            return  # feature not configured

        now = self._now()
        # While the Shelly runs its own reversal sequence the master legitimately
        # reads off. Do not touch the fans; just refresh diagnostics — including
        # the condition-edge notifications, or a sensor loss / overheat that
        # begins during the grace window would never be announced.
        if self.fan_action_grace_until is not None and now < self.fan_action_grace_until:
            prev = (self.fan_sensor_stale, self.fan_overheated, self.fan_breeze_hot)
            self._fan_target()
            self._notify_condition_edges(*prev)
            return

        prev = (self.fan_sensor_stale, self.fan_overheated, self.fan_breeze_hot)
        want_on, want_dir, mode = self._fan_target()
        self._notify_condition_edges(*prev)

        # The master reads off while we believe it should be on: do NOT
        # re-command it. Closing O1 is the Shelly script's re-arm gesture, so
        # re-sending every tick would defeat its own stall latch and keep
        # re-energising a faulted motor — and it would also reset the
        # inferred-fault timer forever, making the latch unreachable. Leave
        # the relay alone until the master comes back or the fault latches.
        master = self.config.get(CONF_FAN_MASTER)
        if want_on and self.fan_master_expected and not self._is_on(master):
            return

        # Fault notification.
        fault_now = self.fan_fault_effective
        if fault_now and not self._fan_fault_notified:
            self._fan_fault_notified = True
            persistent_notification.async_create(
                self.hass,
                "The ceiling fans have latched a fault (stall, low dial or a "
                "failed coil), or the master was switched off unexpectedly. The "
                "fans will not be commanded on. Investigate, then re-arm by "
                "turning the Shelly master on and toggling 'Ceiling fans enabled'.",
                title="🏕 Scout Hut – Ceiling fan fault",
                notification_id=NOTIFY_FAN_FAULT,
            )
        elif not fault_now and self._fan_fault_notified:
            self._fan_fault_notified = False
            persistent_notification.async_dismiss(self.hass, NOTIFY_FAN_FAULT)

        # Fail-safe stops bypass the minimum-run timer; ordinary stops respect it.
        fail_safe_off = (not want_on) and (
            not self.switch_on("fans_enabled", default=True)
            or self.fan_fault_latched
            or self._is_on(self.config.get(CONF_FAN_FAULT))
            or self.fan_sensor_stale
        )

        if want_on and not self.fan_on:
            if self.fan_last_off is not None and (
                now - self.fan_last_off
            ).total_seconds() < self.number("fan_min_off_minutes") * 60:
                return  # honour minimum off time before restarting
        elif (not want_on) and self.fan_on and not fail_safe_off:
            if self.fan_last_on is not None and (
                now - self.fan_last_on
            ).total_seconds() < self.number("fan_min_run_minutes") * 60:
                return  # honour minimum run time before an ordinary stop

        # Cold start (fan_on None) with a physically running master: turning
        # it off is None -> False, which the change detector reads as "no
        # change" — capture the master's real state so that stop is audited
        # too (seen in the field: a restart mid-hold silently stopped the
        # fans, leaving a gap in the log).
        first_sight = self.fan_on is None
        master_was_on = self._is_on(master)
        prev_on, prev_dir = bool(self.fan_on), self.fan_direction
        await self._async_ensure_fans(want_on, want_dir)
        self.fan_mode = mode
        if (
            bool(self.fan_on) != prev_on
            or (self.fan_on and self.fan_direction != prev_dir)
            or (first_sight and master_was_on and not self.fan_on)
        ):
            self.audit.record(
                "fan_change",
                now,
                on=bool(self.fan_on),
                direction=self.fan_direction,
                mode=mode,
                dt=self.fan_dt,
                demand=self.heat_demand,
                occupied=self._fan_occupied,
                warm=self._fan_warm,
                o1_w=self._o1_watts(),
            )

    async def _async_ensure_fans(self, want_on: bool, want_direction: str | None) -> None:
        """Reusable actuator that encodes the hard Shelly rules.

        - Off: open the master.
        - On while master is off: preset the direction relay directly (only legal
          while the master is off), then close the master.
        - On while master is already on but the direction is wrong: press the
          reverse button (the Shelly runs the safe reversal, which ends master
          on). Never write the direction relay while the master is on.
        """
        master = self.config.get(CONF_FAN_MASTER)
        direction = self.config.get(CONF_FAN_DIRECTION)
        reverse = self.config.get(CONF_FAN_REVERSE)
        if not master:
            return

        master_on = self._is_on(master)
        cur_dir = "reverse" if self._is_on(direction) else "forward"
        now = self._now()

        # ---- OFF ----
        if not want_on:
            if master_on:
                await self.hass.services.async_call(
                    "switch", "turn_off", {"entity_id": master}, blocking=False
                )
            self.fan_master_expected = False
            if self.fan_on:
                self.fan_last_off = now
            self.fan_on = False
            self.fan_direction = cur_dir
            persistent_notification.async_dismiss(self.hass, NOTIFY_FAN_DIAL)
            return

        # ---- ON with a target direction ----
        if not master_on:
            # Presetting the direction relay directly is allowed only while the
            # master is off (no load on the coil switching). Give the contactor
            # time to finish travelling before energising, mirroring the settle
            # the Shelly script uses in its own sequence.
            if direction and cur_dir != want_direction:
                await self.hass.services.async_call(
                    "switch",
                    "turn_on" if want_direction == "reverse" else "turn_off",
                    {"entity_id": direction},
                    blocking=False,
                )
                await asyncio.sleep(FAN_DIRECTION_SETTLE)
            await self.hass.services.async_call(
                "switch", "turn_on", {"entity_id": master}, blocking=False
            )
            self.fan_master_expected = True
            self.fan_master_off_since = None
            if not self.fan_on:
                self.fan_last_on = now
            self.fan_on = True
            self.fan_direction = want_direction
            persistent_notification.async_dismiss(self.hass, NOTIFY_FAN_DIAL)
            return

        # master already on
        # Without a mapped direction relay the running direction is unknowable
        # (cur_dir would be a guess), and without the reverse button a live
        # change is impossible. Either way, never attempt a live reversal —
        # blind re-pressing would otherwise cycle the motor through a full
        # reversal sequence on every reconcile. Keep the fans running as they
        # are instead.
        if not direction or not reverse or cur_dir == want_direction:
            self.fan_on = True
            if direction:
                self.fan_direction = cur_dir
            self.fan_master_expected = True
            self._reverse_attempts = 0
            persistent_notification.async_dismiss(self.hass, NOTIFY_FAN_DIAL)
            return

        # Repeated presses without the relay ever changing mean the Shelly
        # script is absent or broken: latch a fault instead of pressing
        # forever (~one full motor reversal attempt every 100 s otherwise).
        if self._reverse_attempts >= MAX_REVERSE_ATTEMPTS:
            if not self.fan_fault_latched:
                self.audit.record("fan_fault", now, reason="reverse_failed")
            self.fan_fault_latched = True
            return
        self._reverse_attempts += 1

        # Live direction change: must go through the reverse button. Remind first
        # to set the dial high (a low dial can stall on reversal); HA cannot check.
        self._notify_dial_high()
        if reverse:
            await self.hass.services.async_call(
                "button", "press", {"entity_id": reverse}, blocking=False
            )
        # The Shelly sequence turns the master off, dwells, flips, then master on.
        # Hold off touching the fans until it finishes.
        self.fan_action_grace_until = now + timedelta(seconds=FAN_REVERSE_GRACE)
        self.fan_master_expected = True
        self.fan_master_off_since = None
        self.fan_on = True
        self.fan_direction = want_direction
        if self.fan_last_on is None:
            self.fan_last_on = now

    def _notify_condition_edges(
        self, prev_stale: bool, prev_hot: bool, prev_breeze: bool
    ) -> None:
        """Raise / dismiss the sensor-lost / overheat / hot-breeze notifications."""
        if self.fan_sensor_stale and not prev_stale:
            self.audit.record("fan_sensor_lost", self._now())
            self._notify_sensor_lost()
        elif prev_stale and not self.fan_sensor_stale:
            persistent_notification.async_dismiss(self.hass, NOTIFY_FAN_SENSOR_LOST)

        # Overheat: past the fan-cooling ceiling a breeze heats people instead
        # of cooling them, so the summer fans are held off.
        if self.fan_overheated and not prev_hot and self._summer_active():
            self.audit.record("overheat_holdoff", self._now(), dt=self.fan_dt)
            persistent_notification.async_create(
                self.hass,
                (
                    f"The hall is at or above {FAN_COOLING_MAX_TEMP:.0f}°C. Air "
                    "this hot blows heat onto people rather than cooling them, "
                    "so the ceiling fans are held off. Ventilate (open windows "
                    "on the shaded side) and encourage drinking water instead."
                ),
                title="🏕 Scout Hut – Too hot for fan cooling",
                notification_id=NOTIFY_FAN_TOO_HOT,
            )
        elif prev_hot and not self.fan_overheated:
            persistent_notification.async_dismiss(self.hass, NOTIFY_FAN_TOO_HOT)

        # Hot-breeze guard: between the useful-breeze ceiling and the hard
        # overheat cutoff, the fans are held and the fix is ventilation.
        if self.fan_breeze_hot and not prev_breeze and self._summer_active():
            self.audit.record("breeze_holdoff", self._now(), mix=self.fan_mix)
            persistent_notification.async_create(
                self.hass,
                (
                    f"The hall air is warm enough (~{self.fan_mix:.0f}°C mixed) "
                    "that a fan breeze no longer helps — it would blow warm air "
                    "onto people. The fans are held off: open doors/windows and "
                    "they resume immediately — and stay running as long as the "
                    "venting is actually cooling the hall."
                    if self.fan_mix is not None
                    else "The hall air is too warm for a useful fan breeze. The "
                    "fans are held off; open doors/windows and they resume "
                    "immediately while the venting is actually cooling the hall."
                ),
                title="🏕 Scout Hut – Too warm for the fans to help; open the doors",
                notification_id=NOTIFY_FAN_BREEZE,
            )
        elif prev_breeze and not self.fan_breeze_hot:
            persistent_notification.async_dismiss(self.hass, NOTIFY_FAN_BREEZE)

    def _notify_dial_high(self) -> None:
        persistent_notification.async_create(
            self.hass,
            "About to reverse the ceiling fans. Set the transformer dial to a "
            "high speed first: a low dial can stall the motor on a direction "
            "change. Home Assistant cannot verify this — it is a reminder only.",
            title="🏕 Scout Hut – Set the fan dial high before reversing",
            notification_id=NOTIFY_FAN_DIAL,
        )

    def _notify_sensor_lost(self) -> None:
        if self.switch_on("fans_run_on_sensor_loss", default=True):
            tail = (
                "Assuming stratification and keeping the winter fans running "
                "while heat is being produced."
            )
        else:
            tail = "The destratification fans are held off until it returns."
        persistent_notification.async_create(
            self.hass,
            "The ceiling or floor temperature reading has been lost or has not "
            "updated recently. " + tail,
            title="🏕 Scout Hut – Fan temperature sensor lost",
            notification_id=NOTIFY_FAN_SENSOR_LOST,
        )
