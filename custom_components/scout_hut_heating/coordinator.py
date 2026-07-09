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
    8. Building empty                      -> ice
"""

from __future__ import annotations

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

from .const import (
    CONF_ALARM_MAIN,
    CONF_ALARM_OFFICE,
    CONF_CALENDAR_HALL,
    CONF_CALENDAR_OFFICE,
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
    CONF_SHARED_CLIMATES,
    CONF_SHARED_WINDOWS,
    CONF_WATER_SWITCH,
    CONF_WEATHER,
    CONF_ZONE_A_DOORS,
    CONF_ZONE_A_WINDOWS,
    CONF_ZONE_B_DOORS,
    CONF_ZONE_B_WINDOWS,
    DOMAIN,
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
    ZONE_A,
    ZONE_B,
)

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
        self._last_apply: dict[str, datetime] = {}

        # Cached calendar look-ahead (refreshed on a slower cadence).
        self.cal_window: dict[str, bool] = {ZONE_A: False, ZONE_B: False}
        self.cal_title: dict[str, str] = {ZONE_A: "", ZONE_B: ""}
        self.water_window = False

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
        ):
            if ent := self.config.get(key):
                watched.append(ent)

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
        preheat = int(self.number("preheat_minutes"))
        water_preheat = int(self.number("water_preheat_minutes"))
        for zone in (ZONE_A, ZONE_B):
            cal = self.config.get(ZONE_CALENDAR[zone])
            if not cal:
                continue
            in_window, title = await self._async_calendar_window(cal, preheat)
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
        labelled ("3-day average"). Returns (avg, engage?, release?).
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
        return avg, avg >= threshold, (avg < threshold or realfeel < threshold)

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
        # Zone quiet but someone is elsewhere in the building: leave as is.
        return None

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

    def _desired_water(self) -> bool | None:
        switch = self.config.get(CONF_WATER_SWITCH)
        if not switch:
            return None
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
            await self._reconcile_shared()
            await self._reconcile_water()
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
            if desired is None or desired == self.applied[zone]:
                if desired is not None:
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
        if desired is None or desired == self.water_on:
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
