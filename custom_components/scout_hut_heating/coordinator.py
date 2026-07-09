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
from homeassistant.util import dt as dt_util

from .fan_logic import fan_decision
from .preheat import required_lead_minutes, updated_rate
from .const import (
    CONF_ALARM_MAIN,
    CONF_ALARM_OFFICE,
    CONF_CALENDAR_HALL,
    CONF_CALENDAR_OFFICE,
    CONF_CEILING_TEMP,
    CONF_FAN_DIRECTION,
    CONF_FAN_FAULT,
    CONF_FAN_MASTER,
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
        self.heat_demand: bool = False             # any radiator drawing power
        self.fan_sensor_stale: bool = False        # ceiling/floor lost
        self.fan_fault_latched: bool = False       # inferred (unpublished) fault
        self.fan_master_expected: bool | None = None  # what we last told O1 to be
        self.fan_master_off_since: datetime | None = None  # for dwell-safe infer
        self.fan_action_grace_until: datetime | None = None  # Shelly mid-sequence
        self._fan_fault_notified: bool = False
        self._discovered_power: list[str] | None = None  # auto-found power sensors
        self._connected_map: dict[str, str] | None = None  # climate -> connected sensor
        # A zone whose last preset was sent while a heater was offline: re-send it
        # once every heater is back online.
        self._zone_offline_apply: dict[str, bool] = {ZONE_A: False, ZONE_B: False}

        # Cached calendar look-ahead (refreshed on a slower cadence).
        self.cal_window: dict[str, bool] = {ZONE_A: False, ZONE_B: False}
        self.cal_title: dict[str, str] = {ZONE_A: "", ZONE_B: ""}
        self.water_window = False

        # Optimum-start learning: an in-flight warm-up sample per zone
        # (started-at, start temperature), and the last comfort target seen on
        # the zone's own heater (used as the office target, which the
        # integration does not otherwise know).
        self._warmup_start: dict[str, tuple[datetime, float] | None] = {
            ZONE_A: None,
            ZONE_B: None,
        }
        self._zone_comfort_target: dict[str, float | None] = {ZONE_A: None, ZONE_B: None}

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
                self.last_motion[motion_entities[entity_id]] = dt_util.utcnow()
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
            self._started = True
            await self._async_refresh_calendars()
            await self._async_seasonal_check()
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
        for zone in (ZONE_A, ZONE_B):
            cal = self.config.get(ZONE_CALENDAR[zone])
            if not cal:
                continue
            in_window, title = await self._async_calendar_window(
                cal, self._zone_preheat_minutes(zone)
            )
            self.cal_window[zone] = in_window or self._is_on(cal)
            if self._is_on(cal):
                state = self.hass.states.get(cal)
                self.cal_title[zone] = (state.attributes.get("message") or "").lower() if state else ""
            else:
                self.cal_title[zone] = title.lower()

        water = False
        for zone in (ZONE_A, ZONE_B):
            cal = self.config.get(ZONE_CALENDAR[zone])
            if not cal:
                continue
            in_window, _ = await self._async_calendar_window(cal, water_preheat)
            water = water or in_window or self._is_on(cal)
        self.water_window = water

    async def _async_calendar_window(self, cal: str, minutes: int) -> tuple[bool, str]:
        """Return (event within window?, first event summary)."""
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
            return self._is_on(cal), ""
        events = (response or {}).get(cal, {}).get("events", []) if response else []
        if events:
            return True, events[0].get("summary", "") or ""
        return False, ""

    # ------------------------------------------------------------------
    # Adaptive pre-heat (optimum start)
    # ------------------------------------------------------------------
    def _zone_room_temp(self, zone: str) -> float | None:
        """Average room temperature reported by a zone's own heaters."""
        if zone == ZONE_A:
            # The hall shares the fan logic's floor reading (explicit floor
            # sensor if mapped, else the hall Rointes' average).
            return self._floor_temp()
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
        return sum(vals) / len(vals) if vals else None

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

    def _zone_preheat_minutes(self, zone: str) -> int:
        """Adaptive pre-heat lead: learned rate x deficit, capped by the slider."""
        minutes = required_lead_minutes(
            rate=self.number(f"{zone}_warmup_rate"),
            indoor=self._zone_room_temp(zone),
            target=self._zone_target(zone),
            outdoor=self._outdoor_temp(),
            max_minutes=self.number("preheat_minutes"),
        )
        return int(round(minutes))

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
                    self._warmup_start[zone] = (now, temp)
                continue

            done = comfort and temp is not None and temp >= target
            if comfort and not done:
                continue  # still warming (or temp reading lost: wait)

            # Warm-up finished (target reached) or ended early (preset left
            # comfort): fold the observation in and clear the sample.
            self._warmup_start[zone] = None
            if temp is None:
                continue
            started, start_temp = sample
            minutes = (now - started).total_seconds() / 60
            rate_key = f"{zone}_warmup_rate"
            new_rate = updated_rate(self.number(rate_key), minutes, temp - start_temp)
            entity = self._numbers.get(rate_key)
            write = getattr(entity, "write_value", None)
            if write is not None and new_rate != self.number(rate_key):
                write(new_rate)

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
        release = avg <= threshold - SEASONAL_RELEASE_BAND or realfeel < threshold
        return avg, avg >= threshold, release

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

    async def async_fan_rearm(self) -> None:
        """Clear an inferred fan fault. This is the deliberate HA-side re-arm.

        Called when the "Ceiling fans enabled" switch is turned on. We never
        auto-rearm inside the loop; a latched fault clears only on this explicit
        gesture (or when a mapped fault boolean clears itself). The physical
        re-arm is still turning the Shelly master on at the device.
        """
        self.fan_fault_latched = False
        self.fan_master_off_since = None
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

    def _desired_zone(self, zone: str) -> str | None:
        enabled_key = f"{zone}_automation_enabled"
        if not self.switch_on(enabled_key, default=True):
            return None
        if self.manual_hold[zone]:
            return None
        if self.opening_ice[zone]:
            return PRESET_ICE
        if self.boost_active(zone):
            return PRESET_COMFORT
        if self.seasonal_lockout:
            return PRESET_ICE

        cal_on = self._cal_active(zone)
        alarm_on = self._is_on(self.config.get(ZONE_ALARM[zone]))
        if alarm_on and not cal_on:
            return PRESET_ICE

        timeout = self.number("motion_timeout_minutes")
        area = ZONE_MOTION_AREA[zone]
        if cal_on:
            base = PRESET_ECO if self._eco_keyword_active(zone) else PRESET_COMFORT
            if base == PRESET_COMFORT and not self._motion_recent(area, timeout):
                return PRESET_ECO
            return base

        if self.switch_on(f"{zone}_occupied_override"):
            return PRESET_ECO
        if self._motion_recent(area, timeout):
            return PRESET_ECO
        if not self._motion_recent_any(timeout):
            return PRESET_ICE
        # Zone quiet but someone is elsewhere in the building: rest at eco rather
        # than leaving a stale comfort preset running (e.g. the hall after a
        # booking ends while a cleaner is still in the kitchen).
        return PRESET_ECO

    def _desired_shared(self) -> str | None:
        if not self.config.get(CONF_SHARED_CLIMATES):
            return None
        if self.seasonal_lockout:
            return PRESET_ICE
        if self.opening_ice["shared"]:
            return PRESET_ICE
        if self.boost_active(ZONE_A) or self.boost_active(ZONE_B):
            return PRESET_COMFORT
        if self._is_on(self.config.get(CONF_ALARM_MAIN)) and self._is_on(
            self.config.get(CONF_ALARM_OFFICE)
        ):
            return PRESET_ICE
        if self._cal_active(ZONE_A) or self._cal_active(ZONE_B):
            return PRESET_ECO
        if self._motion_recent_any(self.number("motion_timeout_minutes")):
            return PRESET_ECO
        return PRESET_ICE

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

    def _desired_water(self) -> bool | None:
        switch = self.config.get(CONF_WATER_SWITCH)
        if not switch:
            return None
        now = self._now()

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
                self.water_frost_active = True
            elif room >= WATER_FROST_OFF_TEMP:
                self.water_frost_active = False
        if self.water_frost_active:
            return True

        # Weekly hygiene heat-up (also overrides the alarms): if the tank has
        # gone a week without a completed reheat, run it long enough for the
        # full 15 L to reach thermostat temperature, so stored water never sits
        # lukewarm indefinitely between lets.
        if self.water_hygiene_until is not None and now < self.water_hygiene_until:
            return True
        self.water_hygiene_until = None
        if self.water_last_hot is None:
            self.water_last_hot = now  # start the clock on first evaluation
        elif now - self.water_last_hot >= WATER_HYGIENE_INTERVAL:
            self.water_hygiene_until = now + timedelta(minutes=WATER_HYGIENE_MINUTES)
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
            await self._reconcile_zones()
            self._update_warmup_learning()
            await self._reconcile_shared()
            await self._reconcile_water()
            await self._reconcile_fans()
            self._detect_drift()
            self._expire_boosts()
            async_dispatcher_send(self.hass, SIGNAL_UPDATE)
        finally:
            self._reconciling = False
        if self._reconcile_pending:
            self._reconcile_pending = False
            await self.async_reconcile()

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
        if desired is None or desired == self.applied["shared"]:
            return
        await self._async_apply_climate(self.config.get(CONF_SHARED_CLIMATES), desired)
        self.applied["shared"] = desired

    async def _reconcile_water(self) -> None:
        desired = self._desired_water()
        if desired is None:
            return
        # Track the continuous powered stretch; _desired_water marks the water
        # as genuinely hot only once it exceeds a full reheat.
        if desired:
            if self.water_on_since is None:
                self.water_on_since = self._now()
        else:
            self.water_on_since = None
        if desired == self.water_on:
            return
        switch = self.config.get(CONF_WATER_SWITCH)
        await self.hass.services.async_call(
            "switch",
            "turn_on" if desired else "turn_off",
            {"entity_id": switch},
            blocking=False,
        )
        self.water_on = desired

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
            if not self._cal_active(zone) or self.boost_active(zone):
                continue
            expected = self.expected_preset[zone]
            if expected is None:
                continue
            # Ignore drift within a minute of our own change (Rointe lag).
            last = self._last_apply.get(zone)
            if last is not None and (self._now() - last).total_seconds() < 60:
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
            if not actual:
                continue
            if actual != expected and not self.manual_hold[zone]:
                self.manual_hold[zone] = True
                persistent_notification.async_create(
                    self.hass,
                    f"Heating was changed manually (expected {expected}, heater "
                    f"is {actual}). Automation is paused until the booking ends.",
                    title=f"🏕 {zone.replace('_', ' ').title()} – Manual control detected",
                    notification_id=NOTIFY_ZONE_HOLD[zone],
                )
            elif actual == expected and self.manual_hold[zone]:
                self.manual_hold[zone] = False
                persistent_notification.async_dismiss(self.hass, NOTIFY_ZONE_HOLD[zone])

    # ------------------------------------------------------------------
    # Applying presets
    # ------------------------------------------------------------------
    async def _async_set_preset(self, zone: str, preset: str, force: bool = False) -> None:
        climates = self._as_list(self.config.get(ZONE_CLIMATES[zone]))
        if not climates:
            return
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
            if self._stale(power, stale_min):
                continue
            value = self._num_state(power)
            if value is not None:
                seen_value = True
                if value > threshold:
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
            for member in er.async_entries_for_device(
                registry, entry.device_id, include_disabled_entities=False
            ):
                if member.domain != "sensor":
                    continue
                eid = member.entity_id.lower()
                is_power = (member.original_device_class or "") == "power" or (
                    "power" in eid and "energy" not in eid
                )
                if is_power:
                    found.append(member.entity_id)
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

    @property
    def fan_fault_effective(self) -> bool:
        """Read-only fault state for diagnostics (never mutates the latch).

        A mapped fault boolean is authoritative both ways: when the Shelly clears
        it (re-armed at the device) the fault clears here too. Only when no
        readable boolean is mapped do we fall back to the inferred latch.
        """
        mapped = self.config.get(CONF_FAN_FAULT)
        if mapped:
            st = self.hass.states.get(mapped)
            if st is not None and st.state not in ("unknown", "unavailable"):
                return st.state == "on"
        return self.fan_fault_latched

    def _fan_fault(self) -> bool:
        """Evaluate (and, for the inferred case, latch) the fan fault.

        A mapped fault boolean wins and is authoritative both ways. Otherwise we
        infer a fault when the master reads off while we expected it on for longer
        than the reverse dwell — but never during the Shelly's own reversal grace.
        The inferred latch never auto-rearms; it clears only via async_fan_rearm.
        """
        mapped = self.config.get(CONF_FAN_FAULT)
        if mapped:
            st = self.hass.states.get(mapped)
            if st is not None and st.state not in ("unknown", "unavailable"):
                return st.state == "on"

        now = self._now()
        in_grace = (
            self.fan_action_grace_until is not None
            and now < self.fan_action_grace_until
        )
        master = self.config.get(CONF_FAN_MASTER)
        if in_grace or not self.fan_master_expected or not master:
            self.fan_master_off_since = None
            return self.fan_fault_latched
        if self._is_on(master):
            self.fan_master_off_since = None
        else:
            if self.fan_master_off_since is None:
                self.fan_master_off_since = now
            elif (now - self.fan_master_off_since).total_seconds() >= FAN_FAULT_GRACE:
                self.fan_fault_latched = True
        return self.fan_fault_latched

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
            return False, None, "off"
        if self._fan_fault():
            return False, None, "off"

        stale_min = self.number("fan_sensor_stale_minutes")
        ceiling_id = self.config.get(CONF_CEILING_TEMP)
        floor_id = self.config.get(CONF_FLOOR_TEMP)
        ct = self._ceiling_temp()
        ft = self._floor_temp(stale_min)
        ceiling_bad = ct is None or self._stale(ceiling_id, stale_min)
        floor_bad = ft is None or (bool(floor_id) and self._stale(floor_id, stale_min))
        sensors_bad = ceiling_bad or floor_bad
        self.fan_sensor_stale = sensors_bad

        if sensors_bad:
            self.fan_dt = None
            dt: float | None = None
        else:
            dt = ct - ft
            self.fan_dt = dt

        warm = None if ft is None else ft > self.number("cooling_temp_high")
        overheated = ft is not None and ft >= FAN_COOLING_MAX_TEMP
        self.fan_overheated = overheated
        # Recirculate residual / leaked ceiling heat while the occupied zone is
        # still below the cap, decoupled from whether a heater is drawing power.
        recirc_ok = ft is not None and ft < self.number("fan_recirc_max_floor_temp")
        timeout = self.number("motion_timeout_minutes")
        occupied = self._motion_recent("hall", timeout) or self._cal_active(ZONE_A)
        demand = self._heat_demand()
        self.heat_demand = demand
        currently_winter = bool(self.fan_on) and self.fan_mode == "winter"

        return fan_decision(
            summer=self._summer_active(),
            occupied=occupied,
            warm=warm,
            overheated=overheated,
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
        # reads off. Do not touch the fans; just refresh diagnostics.
        if self.fan_action_grace_until is not None and now < self.fan_action_grace_until:
            self._fan_target()
            return

        prev_stale = self.fan_sensor_stale
        prev_hot = self.fan_overheated
        want_on, want_dir, mode = self._fan_target()

        # Sensor-lost notification (fires even when the fans keep running).
        if self.fan_sensor_stale and not prev_stale:
            self._notify_sensor_lost()
        elif prev_stale and not self.fan_sensor_stale:
            persistent_notification.async_dismiss(self.hass, NOTIFY_FAN_SENSOR_LOST)

        # Overheat notification: past the fan-cooling ceiling a breeze heats
        # people instead of cooling them, so the summer fans are held off.
        if self.fan_overheated and not prev_hot and self._summer_active():
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

        await self._async_ensure_fans(want_on, want_dir)
        self.fan_mode = mode

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
            persistent_notification.async_dismiss(self.hass, NOTIFY_FAN_DIAL)
            return

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
