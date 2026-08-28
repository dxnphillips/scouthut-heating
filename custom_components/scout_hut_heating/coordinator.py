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
    4. Seasonal lockout                   -> ice, UNLESS a booking (or its
                                             pre-heat) is below its target, which
                                             pierces the lockout and heats
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
from collections import deque
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
from .coast import will_coast_to_target
from .drive import STEP as DRIVE_STEP
from .drive import STEP_INTERVAL_MIN as DRIVE_STEP_INTERVAL_MIN
from .drive import update_drive
from .fan_logic import fan_decision
from .preheat import (
    MAX_COOL_TICK_DROP,
    MAX_RATE,
    MIN_COOL_SAMPLE_DROP,
    MIN_COOL_SAMPLE_GAP,
    MIN_COOL_SAMPLE_HOURS,
    MIN_SAMPLE_MINUTES,
    MIN_SAMPLE_RISE,
    cooling_observed_k,
    cooling_sample_is_outlier,
    hold_margin,
    required_lead_minutes,
    updated_cooling_k,
    updated_rate,
    warmup_observed_rate,
    warmup_rate_is_outlier,
)
from .const import (
    COOLING_DIRECTION_HYST,
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
    FIRE_EVENT,
    FIRE_EVENT_TYPES,
    NOTIFY_CONDENSATION,
    NOTIFY_DRIVE_CAPPED,
    NOTIFY_DRIVE_NO_RESPONSE,
    NOTIFY_DRIVE_REJECTED,
    NOTIFY_FAN_BREEZE,
    NOTIFY_FAN_DIAL,
    NOTIFY_FAN_FAULT,
    NOTIFY_FAN_SENSOR_LOST,
    NOTIFY_FAN_TOO_HOT,
    NOTIFY_INTERNAL_DOOR,
    NOTIFY_FIRE,
    NOTIFY_OPENING_INFERRED,
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

# A LIVE reverse (a spinning fan changing direction) is slow: the heavy blades
# must coast fully to a stop before the Finder interlock lets the opposite
# winding energise, measured at ~5 minutes wall-clock. While it runs the master
# relay legitimately reads off; Home Assistant must stay hands-off for the whole
# sequence and must not mistake the long dwell for a fault. Sized above the
# measurement with margin (a too-short window latches a false fault on a normal
# reversal, which does not auto-clear). A cold start from an already-stopped fan
# does NOT coast — it only waits FAN_DIRECTION_SETTLE for the contactor.
FAN_REVERSE_GRACE = 420  # seconds (~5 min measured reversal + margin)
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

# A booked session (or its pre-heat window) still heats through the seasonal
# lockout when the room is genuinely below the target it is asking for — a cold
# out-of-season booking must not be frozen out. "Cold" is read from the room's
# own coldest heater probe (self-calibrating, no weather constant), so a
# warm-fabric summer booking already at target stays locked out. This band is
# the release hysteresis: once the bypass has warmed the room, it keeps heating
# until the room sits this far ABOVE target, so the pierce cannot flap on/off
# around the setpoint.
COLD_BOOKING_RELEASE_BAND = 0.5  # °C above target before the heat gate releases
# A transient Rointe reading drop-out must not flip a warm room to "wants heat":
# hold the room's own last good reading for this long before falling back to the
# err-warm fail-safe. A cloud blip is seconds; a real outage is far longer.
ROOM_READING_GRACE_MIN = 2.0

# Coast predictor (coast.py) measurement window. The rolling idle-room samples
# span at most PASSIVE_RISE_WINDOW_MIN and a rate is only computed once at least
# PASSIVE_RISE_MIN_SPAN_MIN of idle history exists — long enough for a genuine
# solar/occupancy climb to show through the 0.5 °C reading quanta, short enough
# to still be "current". Below the min span the predictor returns no rate, so
# the pre-heat heats (comfort-lean: no data → heat).
PASSIVE_RISE_WINDOW_MIN = 20.0
PASSIVE_RISE_MIN_SPAN_MIN = 12.0

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
# The Rointe comfort number entities accept up to 30 °C; the drive cap is
# clamped here so a large offset slider can never ask for an out-of-range value.
ROINTE_COMFORT_MAX = 30.0

# Drive-to-target safety net.
# A heater's probe is "lost" for driving if it has not reported within this
# window — the Rointe cloud can freeze while looking alive, so a stale reading
# must not keep driving. A lost probe withdraws that heater to its plain target.
DRIVE_PROBE_STALE_MINUTES = 30.0
# Cross-probe sanity: a probe reading more than this far BELOW its zone's median
# is treated as a glitch and not driven on, so one shorted/stuck sensor cannot
# force a heater to the cap.
DRIVE_PROBE_SANE_BELOW = 4.0  # °C
# Surface a persistent alert once a heater has sat pinned at the drive cap while
# still a full step short of target for this long — a real capacity wall or a
# stuck sensor, either of which the owner wants to know about.
DRIVE_CAP_ALARM_MINUTES = 60.0

# --- Drive self-validation (Q20): does the loop know its commands are working?
# Two independent checks, both reading signals the Rointe cloud cannot fake.
#
# (a) Setpoint read-back. After a push settles, the heater's own REPORTED
# setpoint should match what we pushed; a persistent divergence is "the heater
# isn't accepting our setpoint" (the v1.14.2 phantom-push class), a distinct
# fault from "can't reach target" (the cap alarm above). The settle window is
# set well above the real Rointe cloud lag so ordinary lag can never trip it.
# Raised 10 -> 30 (v1.24.4, field): two booked-evening exports showed the *number*
# entity adopting our push instantly but the climate's *live setpoint* — what the
# read-back reads — taking 10-27 min to catch up through the cloud (a heater
# flagged at 10 min was matched and heating to 21.0 by the 27-min export). The
# device IS adopting, just slowly, so 30 min clears the observed lag while a
# genuine never-adopt stays mismatched past it and is still caught.
DRIVE_SETTLE_MINUTES = 30.0
DRIVE_SETPOINT_TOL = 0.3  # °C; our pushes and the Rointe are 0.5-quantised
# Grace after (re)start before the drive self-checks may fire. The Rointe cloud
# is much slower to reflect a pushed setpoint just after a restart than in
# steady state: a 2026-08-07 export caught the read-back flagging all four hall
# heaters ~10 min after a restart (mid "wrapping up startup"), stuck reporting
# the OLD setpoint — then they matched once the cloud caught up. So the settle
# window alone is not enough right after boot; abstain until the integration has
# been up this long. Generous on purpose (fail-safe: abstain longer, never flag
# sooner), matching the other over-sized self-check windows.
DRIVE_STARTUP_GRACE_MINUTES = 25.0
# (b) Independent no-response witness. The ceiling thermometer is an independent
# instrument: if the hall is boosting hard (a heater pushed above its plain
# target) with heat demand on yet, over this window, NEITHER the floor probes
# NOR the ceiling move at all, the requested heat is reaching nothing anywhere —
# a dead chain (phantom push, total outage), which a capacity wall is not (a
# capacity wall still warms the ceiling — stratification). Long window + a real
# movement epsilon so a room already holding steady at target never trips it.
DRIVE_NO_RESPONSE_MINUTES = 45.0
DRIVE_NO_RESPONSE_EPS = 0.3  # °C of movement that counts as "responding"

_LOGGER = logging.getLogger(__name__)

SIGNAL_UPDATE = f"{DOMAIN}_update"

# Per-zone entity-map keys.
ZONE_CLIMATES = {ZONE_A: CONF_HALL_CLIMATES, ZONE_B: CONF_OFFICE_CLIMATES}
ZONE_CALENDAR = {ZONE_A: CONF_CALENDAR_HALL, ZONE_B: CONF_CALENDAR_OFFICE}
ZONE_ALARM = {ZONE_A: CONF_ALARM_MAIN, ZONE_B: CONF_ALARM_OFFICE}
# Only an *away*-type arm means the building is empty and heating should drop to
# ice. `armed_night`/`armed_home` mean people are inside (asleep) — heat stays
# on — and `triggered`/`arming`/`pending`/`disarmed` never suppress. A legacy
# binary_sensor/input_boolean mapping is handled separately (its "on" = armed).
ALARM_AWAY_STATES = frozenset({"armed_away", "armed_vacation"})
# The opposite: an arm that says people are INSIDE (a sleepover / present but
# still), which the hall PIR cannot see. A positive "occupied" signal that keeps
# a running booking at comfort instead of demoting to eco on motion-silence.
ALARM_PRESENT_STATES = frozenset({"armed_night", "armed_home"})
# Zones whose heaters the drive-to-target loop controls, and where their
# climate entities are mapped. The shared zone is included so every heated room
# is driven to its comfort target, not just the hall.
DRIVE_ZONE_CLIMATES = {
    ZONE_A: CONF_HALL_CLIMATES,
    ZONE_B: CONF_OFFICE_CLIMATES,
    "shared": CONF_SHARED_CLIMATES,
}
DRIVE_COMFORT_TARGET_KEY = {
    ZONE_A: "hall_comfort_temp",
    ZONE_B: "office_comfort_temp",
    "shared": "shared_comfort_temp",
}
ZONE_DOORS = {ZONE_A: CONF_ZONE_A_DOORS, ZONE_B: CONF_ZONE_B_DOORS}
ZONE_WINDOWS = {ZONE_A: CONF_ZONE_A_WINDOWS, ZONE_B: CONF_ZONE_B_WINDOWS}
ZONE_MOTION_AREA = {ZONE_A: "hall", ZONE_B: "office"}
ZONE_LABEL = {ZONE_A: "hall", ZONE_B: "office"}

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
        # Occupant "too warm, stop" cutout for the hall (radiators are locked,
        # so this is the only accessible way to kill the heat). Forces the hall
        # to ice — still frost-protected — above boost and bookings. No timer:
        # it clears only on a deliberate action (resume/boost) or when a new
        # session emerges from an idle gap (see _async_refresh_calendars /
        # _record_booking_edges). Persisted so a restart mid-pause keeps it.
        self.hall_heating_paused = False
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
        # Whether the fans wanted the cooling (forward) regime this tick — used
        # to gate the overheat/breeze notifications (they only matter when the
        # fans are trying to cool). State-derived, replacing the old season flag.
        self._fan_cooling_wanted: bool = False
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
        # The event start the pre-heat window is currently latched open for.
        # The latch holds the window open through the room warming up (so `lead`
        # shrinking cannot flap comfort<->ice), but is keyed to the SPECIFIC
        # event so it does NOT bridge one booking's pre-heat into the next: a
        # running event and an empty look-ahead both clear it, and a new first
        # event re-evaluates `gap <= lead` fresh (else back-to-back bookings
        # inside the look-ahead would hold comfort continuously across the gap).
        self._preheat_open_for: dict[str, datetime | None] = {ZONE_A: None, ZONE_B: None}
        self.water_window = False
        # The (comfort, eco) setpoint pair last written to the hall heaters, so
        # the reconciler can re-assert the eco/eco-low value when it changes
        # WITHOUT a preset transition (an eco-keyword booking inheriting an
        # already-eco preset would otherwise never get eco-low written). None
        # until the first push.
        self._hall_temps_pushed: tuple[float, float] | None = None

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
            str,
            tuple[datetime, float, float, int, int, int, float, int, float, float]
            | None,
        ] = {
            ZONE_A: None,
            ZONE_B: None,
        }
        self._zone_comfort_target: dict[str, float | None] = {ZONE_A: None, ZONE_B: None}
        # Last good room reading per zone (time, temp), so a transient Rointe
        # drop-out holds the recent truth instead of erring warm on a blip.
        self._last_room_temp: dict[str, tuple[datetime, float] | None] = {
            ZONE_A: None,
            ZONE_B: None,
        }
        # Latch per zone: an out-of-family cool-off (loss implausibly above the
        # learned fabric baseline) means an unsensored opening — push once on the
        # rising edge, clear on the next in-family sample (window closed).
        self._opening_inferred: dict[str, bool] = {ZONE_A: False, ZONE_B: False}

        # Rolling (timestamp, coldest-hall-reading) samples for the "will it get
        # there on its own?" coast predictor (coast.py). Only accumulated while
        # the heaters are IDLE (no heat demand) so the measured rise is genuine
        # free gain (sun / occupancy / fabric), never the radiators' own work —
        # cleared the moment demand appears, which also stops the predictor
        # chattering the heat on and off. Not persisted: a fresh idle window
        # rebuilds in minutes and a stale cross-restart slope would be
        # meaningless. `_coasting` latches the per-zone decision so the audit
        # event fires once on entry, not every tick.
        self._passive_rise: deque[tuple[datetime, float]] = deque()
        self._coasting: dict[str, bool] = {ZONE_A: False, ZONE_B: False}

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
        # Last fan transformer-tap wattage seen while the fans were genuinely
        # running. Through a pre-heat idle gap the Shelly master is off (O1
        # reads zero), so the live power cannot say which fan speed the
        # optimistic fan-assisted warm-up rate is implicitly assuming — this
        # remembers the tap the fans were last at, recorded on preheat_start /
        # hall booking_start for shortfall-vs-speed analysis (open question 14).
        self._fan_w_last_seen: float | None = None
        # Previous running state per calendar; None = not yet observed, so a
        # restart mid-booking does not audit a phantom booking start.
        self._cal_running_prev: dict[str, bool | None] = {ZONE_A: None, ZONE_B: None}
        # Why each zone's desired preset is what it is (the rung of the
        # priority ladder that decided it), stashed by _desired_zone/_shared
        # so preset audit events can say WHY, not just what.
        self._preset_reason: dict[str, str] = {}

        # Drive-to-target (outer per-heater trim loop). Keyed by climate
        # entity_id. The staircase term is deliberately NOT persisted across a
        # restart: startup resets the device setpoints to their plain targets
        # first, so a crash can never leave a wound-up overdrive behind.
        self._drive_stair: dict[str, float] = {}
        self._drive_step_at: dict[str, datetime] = {}
        self._drive_pushed: dict[str, float] = {}  # last setpoint pushed per heater
        self._drive_number: dict[str, str] = {}  # cached climate -> comfort number
        self._drive_cap_since: dict[str, datetime | None] = {}  # cap-pinned clock
        self._drive_notified = False  # cap-pinned alert raised
        # Drive self-validation (Q20). When each heater was last pushed a NEW
        # setpoint (to time the read-back settle window), and which heaters are
        # currently failing the read-back. `_drive_response_ref` anchors the
        # hall's independent no-response witness: (captured-at, floor, ceiling)
        # while boosting hard, reset whenever either moves. Notification latches
        # so the alerts raise/clear once, not every tick.
        self._drive_pushed_at: dict[str, datetime] = {}
        # Heaters currently in the driven (comfort) state. A heater that flips
        # OUT of comfort and back must restart its read-back settle window and
        # re-assert the comfort preset — otherwise a stale settle stamp from a
        # withdrawal (which writes the comfort number while the heater is on ice)
        # makes the read-back judge it before it has adopted the freshly-applied
        # comfort setpoint, a false `drive_setpoint_rejected`.
        self._drive_driven: set[str] = set()
        self._drive_rejected: set[str] = set()
        self._drive_reject_notified = False
        self._opening_notified: set[str] = set()
        # Fire fallback latch: on a panel fire the fans are hardware-cut, but this
        # holds ALL of it off in software (heating -> ice, water off, fans off)
        # until a person clears it — so a power blip cannot re-arm the fans mid-
        # fire, and the whole hut stays off until someone confirms it is safe.
        self._fire_hold: bool = False
        self._fire_notified: bool = False
        self._drive_response_ref: tuple[datetime, float | None, float | None] | None = None
        self._drive_noresp_notified = False

        # Durable state (safety latches and long clocks survive a restart).
        self._store: Store = Store(hass, 1, f"{DOMAIN}_{entry.entry_id}")

        self._unsubs: list = []
        self._started = False
        self._started_at: datetime | None = None
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

        fan_master = self.config.get(CONF_FAN_MASTER)

        @callback
        def _handle_state_event(event: Event) -> None:
            entity_id = event.data.get("entity_id")
            new_state = event.data.get("new_state")
            if entity_id in motion_entities and new_state is not None and new_state.state == "on":
                self._feed_motion(motion_entities[entity_id], dt_util.utcnow())
            if entity_id == fan_master:
                self._note_fan_master_state(new_state.state if new_state else None)
            self.async_request_reconcile()

        if watched:
            self._unsubs.append(
                async_track_state_change_event(self.hass, watched, _handle_state_event)
            )

        # Fire fallback: listen for the alarm integration's bus event. No-op if
        # that integration is not installed (the event simply never fires).
        self._unsubs.append(
            self.hass.bus.async_listen(FIRE_EVENT, self._handle_fire_event)
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
            # Undo any drive overdrive a crash may have left on the heaters
            # BEFORE the first reconcile: reset every driven setpoint to its
            # plain target, then the loop re-drives from zero on live readings.
            if self.switch_on("drive_to_target", default=True):
                await self.async_drive_reset()
            self._started = True
            self._started_at = self._now()
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

    def _alarm_armed(self, entity_id: str | None) -> bool:
        """True when the mapped alarm says the building is EMPTY (away-armed).

        Reads a real ``alarm_control_panel`` so an *away*-type arm suppresses
        heating while *night*/*home* (people sleeping/present inside) does not —
        that is what keeps the heat on for a Night-armed sleepover. A legacy
        ``binary_sensor``/``input_boolean`` mapping still works: its ``on`` is
        treated as armed-away, preserving pre-1.12 behaviour for installs that
        feed the alarm through a helper. ``triggered``/``arming``/``pending``/
        ``disarmed``/unknown never suppress.
        """
        if not entity_id:
            return False
        state = self.hass.states.get(entity_id)
        if state is None:
            return False
        return state.state == "on" or state.state in ALARM_AWAY_STATES

    def _alarm_present(self, entity_id: str | None) -> bool:
        """True when the mapped alarm is *night*/*home* armed — people are inside
        (a sleepover), which the PIR cannot see when they are still. A positive
        presence signal, distinct from the empty-building away-arm."""
        if not entity_id:
            return False
        state = self.hass.states.get(entity_id)
        return state is not None and state.state in ALARM_PRESENT_STATES

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
                # The event is running, not pre-heating: drop the pre-heat latch
                # so that when this event ends the NEXT event's pre-heat is
                # re-evaluated fresh rather than inheriting this one's open window.
                self._preheat_open_for[zone] = None
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
                self._coasting[zone] = False
                self._preheat_open_for[zone] = None
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
                    if zone == ZONE_A:
                        self._clear_hall_pause("preheat")
                self.cal_window[zone] = True
                continue
            lead = self._zone_preheat_minutes(zone, eco=eco, gap_hours=gap_min / 60)
            # Latch the window OPEN once it has opened FOR THIS EVENT, rather than
            # re-deciding the boundary every refresh. `lead` shrinks as the room
            # warms, so a bare `gap <= lead` test lets the window close again the
            # moment the room nears target — flipping the zone out of comfort and
            # back (observed 2026-08-05: comfort->ice->comfort during a near-target
            # pre-heat, amplified under the seasonal-lockout pierce). The latch is
            # keyed to the event's start (`_preheat_open_for`), so it holds this
            # pre-heat open but does NOT bridge into the NEXT booking: a running
            # event / empty look-ahead clears the key, and a fresh first event is
            # judged on `gap <= lead` again (else two bookings inside the look-ahead
            # would hold comfort continuously across the empty gap between them).
            latched = start is not None and self._preheat_open_for.get(zone) == start
            window = gap_min <= lead or latched
            if window and not self.cal_window[zone]:
                self.audit.record(
                    "preheat_start",
                    self._now(),
                    zone=zone,
                    **(self._last_lead_calc.get(zone) or {}),
                )
                # A session emerging from an idle gap is the deliberate signal to
                # lift a hall pause (the too-warm occupants have gone; warm the
                # incoming group). Mid-booking this edge never fires — the window
                # is already open — so adjacent bookings clear at booking_end.
                if zone == ZONE_A:
                    self._clear_hall_pause("preheat")
            self.cal_window[zone] = window
            self._preheat_open_for[zone] = start if window else None

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
    def _zone_climate_temps(
        self, zone: str, stale_min: float | None = None
    ) -> list[float]:
        """All readable room temperatures from a zone's own heaters.

        ``stale_min``: drop a heater whose reading has not updated within this
        many minutes. The Rointe cloud can FREEZE while the entity still reads
        ``available`` (CLAUDE.md: "readings can freeze while looking alive"), so
        any path that decides whether the room is warm enough — pre-heat sizing,
        the cold-booking pierce, the summer setback — must reject a frozen value
        rather than trust it (a stale-high reading otherwise under-leads a cold
        start into a cold arrival). Omit it where a frozen value is harmless
        (the fan ΔT reference, the diagnostic spread).
        """
        vals: list[float] = []
        for climate in self._as_list(self.config.get(ZONE_CLIMATES[zone])):
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
        return vals

    def _zone_room_temp(
        self, zone: str, coldest: bool = False, stale_min: float | None = None
    ) -> float | None:
        """Room temperature reported by a zone's own heaters.

        ``coldest=True`` returns the lowest reading instead of the average:
        the hall units disagree by several degrees along the 20 m room, and
        for "will the room be warm enough?" questions (pre-heat sizing) the
        coldest reading is the truer measure of the far end. The average
        stays right for the fan ΔT reference and the learning, where
        stability against a single odd sensor matters more. ``stale_min`` (see
        ``_zone_climate_temps``) rejects a frozen Rointe reading on the
        warm-enough decision paths.
        """
        vals = self._zone_climate_temps(zone, stale_min=stale_min)
        if coldest and vals:
            return min(vals)
        if zone == ZONE_A:
            # The hall average shares the fan logic's floor reading (explicit
            # floor sensor if mapped, else the hall Rointes' average).
            return self._floor_temp(stale_min=stale_min)
        return sum(vals) / len(vals) if vals else None

    def _rointe_stale_min(self) -> float:
        """The window (minutes) after which an un-updated Rointe reading is
        treated as frozen on the warm-enough decision paths — reuses the fan
        floor-staleness slider so there is one Rointe freshness knob, not two."""
        return self.number("fan_sensor_stale_minutes")

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
        heaters). For the office: once the drive-to-target loop owns and pushes
        the office comfort setpoint, its slider IS the target; otherwise the
        setpoint lives on the Rointe, so the last target seen while the office
        was actually in comfort is cached and used (falling back to the hall
        slider until one has been seen).
        """
        if zone == ZONE_A:
            return self.number("hall_comfort_temp")
        if zone == ZONE_B and self.switch_on("drive_to_target", default=True):
            return self.number("office_comfort_temp")
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
        rate, rate_key = self._prediction_rate(zone)
        # Size the pre-heat for the coldest reading, not the average: the
        # warm end's heater must not cut the lead short for the cold end. Reject
        # a frozen Rointe reading (stale_min) — a stale-high value would
        # under-lead a cold start; None falls back to the cap, i.e. fail-warm.
        indoor = self._zone_room_temp(zone, coldest=True, stale_min=self._rointe_stale_min())
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
            # Which learned rate drove the lead: "zone_a_warmup_rate_fans" is
            # the optimistic fan-assisted path whose speed assumption is only
            # verifiable once the Shelly turns on. Paired with fan_w_last, this
            # is what a cold-arrival shortfall gets read against.
            "rate_key": rate_key,
            "fan_w_last": self._fan_w_last_seen if zone == ZONE_A else None,
            "indoor_coldest": indoor,
            "target": target,
            "outdoor": outdoor,
            "loss_pct": loss_pct,
        }
        return lead

    def _prediction_rate(self, zone: str) -> tuple[float, str]:
        """The learned rate to predict a warm-up with, and the key it came from.

        The fan-assisted hall rate is preferred when the fans are expected to
        help, but only once it has actually been trained: at its untouched
        fail-safe seed (MAX_RATE) it would pin every lead at the cap forever
        while the observations were all landing in the base rate. Until then,
        fall back to whichever knowledge exists. The key returned is the one
        that ACTUALLY drove the value (so the audit's rate_key matches the
        rate), not merely the one that was preferred — Q14 reads them paired.
        """
        key = self._warmup_rate_key(zone)
        rate = self.number(key)
        if key == "zone_a_warmup_rate_fans" and rate >= MAX_RATE:
            base = self.number("zone_a_warmup_rate")
            if base < rate:
                rate, key = base, "zone_a_warmup_rate"
        return rate, key

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
            # During any heated warm-up the hall is on a heating preset, which
            # forces the fans to reverse/destrat (they assist) — so the fans help
            # whenever they are enabled, regardless of season (the old
            # `not _summer_active()` proxy under-counted a summer cold-booking
            # pierce, whose pre-heat fans DO assist).
            assisted = self.switch_on("fans_enabled", default=True)
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

    def _note_fan_speed(self) -> None:
        """Remember the transformer tap while the fans are genuinely running.

        The tap is a manual dial HA cannot command; through a pre-heat idle
        gap the master is off and O1 reads zero, so this last-seen value is the
        only record of which speed the fan-assisted pre-heat prediction is
        leaning on. Only overwrite on a fan-scale reading — a None/zero draw is
        the fans stopped, not a new (slower) setting.
        """
        if not self._fans_running():
            return
        w = self._o1_watts()
        if w is not None and w > FAN_RUNNING_MIN_WATTS:
            self._fan_w_last_seen = w

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
            # Free gain (solar/occupancy/fan-delivered ceiling heat) makes a
            # warm-up read implausibly fast; folding it would corrupt the rate
            # LOW and shorten the lead toward a cold arrival. Rejected by
            # updated_rate; flagged here for the audit (no push — the sun helping
            # is not something to act on).
            quality = rise >= MIN_SAMPLE_RISE and minutes >= MIN_SAMPLE_MINUTES
            observed = warmup_observed_rate(minutes, rise) if quality else None
            outlier = (
                quality
                and observed is not None
                and warmup_rate_is_outlier(old_rate, observed)
            )
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
                accepted=quality and not outlier,
                outlier=outlier,
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

            def _anchor(
                anchor_temp: float,
            ) -> tuple[datetime, float, float, int, int, int, float, int, float, float]:
                w = self._o1_watts() if zone == ZONE_A else None
                return (
                    now,
                    anchor_temp,
                    outdoor if outdoor is not None else 0.0,
                    1 if outdoor is not None else 0,
                    1 if self._fans_running() else 0,
                    1,
                    w or 0.0,
                    1 if w is not None else 0,
                    anchor_temp,  # prev_temp: last tick's reading
                    0.0,  # max_tick_drop: largest single-tick fall so far
                )

            if sample is None:
                if cooling and temp is not None:
                    self._cooloff_start[zone] = _anchor(temp)
                continue

            (
                started, start_temp, out_sum, out_n, fan_ticks, ticks,
                watt_sum, watt_n, prev_temp, max_tick_drop,
            ) = sample
            # Accumulate the outdoor reading every tick: the sample's average
            # gap is what normalises the observed loss into the constant. The
            # fan tally rides along because a fan-mixed cool-off measurably
            # differs from a still one (2026-07-11 sealed test: mixing roughly
            # halved the gap-normalised loss) — recorded, not yet acted on. The
            # O1 wattage rides along too: the tap fingerprint is
            # direction-dependent (summer forward ~195 W vs winter reverse
            # ~158 W), so a future fan-aware split needs the speed, not just the
            # count, to tell winter recirculation cool-offs apart.
            if outdoor is not None:
                out_sum += outdoor
                out_n += 1
            fan_ticks += 1 if self._fans_running() else 0
            ticks += 1
            w = self._o1_watts() if zone == ZONE_A else None
            if w is not None:
                watt_sum += w
                watt_n += 1
            # Track the largest single-tick fall. A genuine fabric cool-off
            # eases the reading down one 0.5 °C quantum at a time, so a lone
            # tick shedding >= MAX_COOL_TICK_DROP is a discontinuity — an
            # unmonitored open door/window (the office has no contact to raise
            # the opening guard) or the Rointe probe unfreezing — that makes
            # the sample's rate uninterpretable; _fold_cooloff rejects on it.
            if temp is not None:
                tick_drop = prev_temp - temp
                if tick_drop > max_tick_drop:
                    max_tick_drop = tick_drop
                prev_temp = temp
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
                        watt_sum,
                        watt_n,
                        max_tick_drop,
                    )
                continue

            if temp > start_temp + 0.3:
                self._cooloff_start[zone] = _anchor(temp)  # gaining, not losing
                continue
            self._cooloff_start[zone] = (
                started, start_temp, out_sum, out_n, fan_ticks, ticks,
                watt_sum, watt_n, prev_temp, max_tick_drop,
            )
            drop = start_temp - temp
            hours = (now - started).total_seconds() / 3600
            # Roll the window ONLY when the sample is long enough to be
            # accepted: re-anchoring on a rejected (too-short) sample would
            # create a dead zone where fast heat loss (reaching the drop
            # trigger in under the minimum duration) could never be learned.
            if drop >= MIN_COOL_SAMPLE_DROP and hours >= MIN_COOL_SAMPLE_HOURS:
                self._fold_cooloff(
                    zone, hours, drop, start_temp, temp, out_sum, out_n,
                    fan_ticks, ticks, watt_sum, watt_n, max_tick_drop,
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
        watt_sum: float,
        watt_n: int,
        max_tick_drop: float = 0.0,
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
                o1_avg_w=(watt_sum / watt_n) if watt_n else None,
            )
            return
        gap = (start_temp + end_temp) / 2 - out_sum / out_n
        k = current / 100
        new_k = updated_cooling_k(k, hours, drop, gap, max_tick_drop)
        new = current if new_k == k else new_k * 100
        # A quality sample whose raw loss is implausibly above the learned baseline
        # is an unsensored opening (or a probe glitch), not the fabric — it was NOT
        # folded in (updated_cooling_k rejected it), and the latch drives the
        # "window/door open?" push. A quality, in-family sample clears the latch
        # (the opening closed); a poor-quality/noise sample leaves it untouched.
        quality_ok = (
            drop >= MIN_COOL_SAMPLE_DROP
            and hours >= MIN_COOL_SAMPLE_HOURS
            and gap >= MIN_COOL_SAMPLE_GAP
            and max_tick_drop < MAX_COOL_TICK_DROP
        )
        observed = cooling_observed_k(hours, drop, gap)
        outlier = quality_ok and observed is not None and cooling_sample_is_outlier(k, observed)
        if quality_ok:
            self._opening_inferred[zone] = outlier
        self.audit.record(
            "cooloff_sample",
            self._now(),
            zone=zone,
            hours=hours,
            drop=drop,
            gap=gap,
            accepted=quality_ok and not outlier,
            outlier=outlier,
            old_pct=current,
            new_pct=new,
            fan_ticks=fan_ticks,
            ticks=ticks,
            max_tick_drop=max_tick_drop,
            o1_avg_w=(watt_sum / watt_n) if watt_n else None,
        )
        entity = self._numbers.get(key)
        write = getattr(entity, "write_value", None)
        if write is not None and new != current:
            write(new)

    async def _update_opening_inferred_alarm(self) -> None:
        """Surface an inferred unsensored opening (rising edge), clear on close.

        The learning already protected itself (the out-of-family sample was not
        folded); this tells a human WHY a zone is bleeding heat, on the zones we
        cannot sense directly (the office has no contact). Fires a persistent
        notification AND a push to every companion-app device, once per episode.
        """
        for zone in (ZONE_A, ZONE_B):
            inferred = self._opening_inferred.get(zone, False)
            notified = zone in self._opening_notified
            if inferred and not notified:
                self._opening_notified.add(zone)
                self.audit.record(
                    "opening_inferred",
                    self._now(),
                    zone=zone,
                    heatloss_pct=self.number(f"{zone}_heatloss_pct"),
                )
                title = "🏕 Scout Hut – window or door open?"
                message = (
                    f"The {ZONE_LABEL[zone]} is losing heat far faster than its "
                    "fabric can — a window or door has probably been left open. "
                    "The reading was ignored so it can't corrupt the learned "
                    "heat-loss, but it's worth a check."
                )
                persistent_notification.async_create(
                    self.hass,
                    message,
                    title=title,
                    notification_id=NOTIFY_OPENING_INFERRED[zone],
                )
                await self._push_companion(
                    title, message, icon="mdi:door-open", channel="Scout Hut Openings"
                )
            elif not inferred and notified:
                self._opening_notified.discard(zone)
                persistent_notification.async_dismiss(
                    self.hass, NOTIFY_OPENING_INFERRED[zone]
                )

    async def _push_companion(
        self,
        title: str,
        message: str,
        *,
        icon: str = "mdi:home-alert",
        channel: str = "Scout Hut alerts",
    ) -> None:
        """Push to every device with the Home Assistant companion app installed.

        Enumerates the ``notify.mobile_app_*`` services the companion app
        registers per device and calls each. No-op when none are installed (the
        persistent notification still shows). Best-effort: a failing or missing
        target never breaks the reconcile.

        Only genuinely urgent alerts reach this path (a fire hold, a window/door
        left open), so every push is flagged for **Android Auto** — ``car_ui``
        surfaces it on the car display so it's seen while driving, and ``ttl``/
        ``priority`` force immediate delivery even if the phone is dozing. The
        per-alert ``channel`` lets the owner tune each one (set it to pop up on
        the phone, which is what makes it appear on top of the car screen). The
        companion app must stay in the Android Auto launcher for ``car_ui`` to
        work.
        """
        data = {
            "car_ui": True,
            "notification_icon": icon,
            "channel": channel,
            "importance": "high",
            "ttl": 0,
            "priority": "high",
        }
        try:
            services = self.hass.services.async_services().get("notify", {})
        except Exception:  # noqa: BLE001
            return
        for service in list(services):
            if not str(service).startswith("mobile_app_"):
                continue
            try:
                await self.hass.services.async_call(
                    "notify",
                    service,
                    {"title": title, "message": message, "data": data},
                    blocking=False,
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("companion push to %s failed: %s", service, err)

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
                    f"The next 3 days average {avg:.1f}°C (warm-season threshold "
                    f"{threshold:.0f}°C), so the hut is in its warm season and the "
                    "condensation watch is paused. Heating is unaffected — it still "
                    "follows bookings and occupancy whenever a room is genuinely cold."
                ),
                title="🏕 Scout Hut – Warm season",
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
        # Boost and the hall pause are opposite intents; the newer one wins. A
        # hall boost is an explicit "I want heat now", so it lifts the pause.
        if zone == ZONE_A:
            self._clear_hall_pause("boost")
        await self.async_reconcile()

    async def async_cancel_boost(self, zone: str) -> None:
        self.boost_until[zone] = None
        await self.async_reconcile()

    async def async_pause_hall_heating(self) -> None:
        """Occupant cutout: force the hall to ice until a deliberate resume."""
        if not self.hall_heating_paused:
            self.audit.record(
                "heating_paused",
                self._now(),
                zone=ZONE_A,
                coldest=self._zone_room_temp(ZONE_A, coldest=True),
                ceiling=self._ceiling_temp(),
                occupied=self._cooling_occupied(),
            )
        self.hall_heating_paused = True
        # A pause and a boost cannot both hold: stopping the heat cancels a boost.
        self.boost_until[ZONE_A] = None
        await self.async_reconcile()

    async def async_resume_hall_heating(self) -> None:
        self._clear_hall_pause("manual")
        await self.async_reconcile()

    def _clear_hall_pause(self, reason: str) -> None:
        """Lift the hall cutout (if set) and audit why."""
        if self.hall_heating_paused:
            self.hall_heating_paused = False
            self.audit.record("heating_resumed", self._now(), zone=ZONE_A, reason=reason)

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
            "hall_heating_paused": self.hall_heating_paused,
            # A fire hold must survive a restart: if HA reboots mid-fire the hut
            # must come back still holding everything off, not resume heating.
            "fire_hold": self._fire_hold,
            # Persisted so a restart mid-booking does not read a phantom
            # idle->session (False->True) edge and clear the hall pause: the
            # window's pre-restart value is restored, so a running booking
            # reads True->True (no edge). Mirrors the _cal_running_prev guard
            # the booking-edge path already uses against the same hazard.
            "cal_window": dict(self.cal_window),
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
        self.hall_heating_paused = bool(data.get("hall_heating_paused", False))
        self._fire_hold = bool(data.get("fire_hold", False))
        saved_window = data.get("cal_window") or {}
        for zone in (ZONE_A, ZONE_B):
            if zone in saved_window:
                self.cal_window[zone] = bool(saved_window[zone])
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
                    # The device's ACTIVE target setpoint and heating action —
                    # the ground truth for whether a drive push actually landed
                    # (drive.pushed is only what we SENT). Without these the
                    # setpoint-not-landing failure looks identical to a capacity
                    # wall in the export.
                    "setpoint": (st.attributes.get("temperature") if st else None),
                    "action": (st.attributes.get("hvac_action") if st else None),
                    "online": self._climate_online(climate),
                    # The comfort-temperature NUMBER the drive writes to, plus its
                    # value and allowed max — so a "setpoint not landing" can be
                    # split three ways: the number accepted our value but the
                    # climate setpoint did not follow (preset-reset), the number
                    # clamped our value (range too low), or the write never took.
                    "comfort_number": self._comfort_number_state(climate),
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

        # Raw door/window contact states, per group. The `opening_ice` flags
        # above are a *derived* 10-minute held-open latch, not the live door
        # state, and the breeze vent pass reads these raw contacts instead —
        # so exporting them makes both decisions legible (an open door with
        # `opening_ice` still false is a door not yet held past the delay, or
        # one opening and closing during traffic). Unmapped groups show as
        # empty; `any_open` mirrors the exact signal the vent override uses.
        opening_groups = {
            "zone_a_doors": CONF_ZONE_A_DOORS,
            "zone_a_windows": CONF_ZONE_A_WINDOWS,
            "zone_b_doors": CONF_ZONE_B_DOORS,
            "zone_b_windows": CONF_ZONE_B_WINDOWS,
            "shared_windows": CONF_SHARED_WINDOWS,
        }
        openings: dict[str, Any] = {
            name: {eid: self._is_on(eid) for eid in self._as_list(self.config.get(key))}
            for name, key in opening_groups.items()
        }
        internal = self.config.get(CONF_INTERNAL_DOOR)
        openings["internal_door"] = {internal: self._is_on(internal)} if internal else {}
        openings["any_open"] = self._any_opening_open()

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
                "opening_inferred": dict(self._opening_inferred),
                "last_lead_calc": self._last_lead_calc,
            },
            "state": {
                "applied": dict(self.applied),
                "expected": dict(self.expected_preset),
                "manual_hold": dict(self.manual_hold),
                "opening_ice": dict(self.opening_ice),
                "openings": openings,
                "boost_until": {z: _iso(t) for z, t in self.boost_until.items()},
                "hall_heating_paused": self.hall_heating_paused,
                "fire_hold": self._fire_hold,
                "seasonal_lockout": self.seasonal_lockout,
                "cal_window": dict(self.cal_window),
                "cal_title": dict(self.cal_title),
                "drive": {
                    "enabled": self.switch_on("drive_to_target", default=True),
                    "pushed": dict(self._drive_pushed),
                    "stair": dict(self._drive_stair),
                    "capped_alert": self._drive_notified,
                    "setpoint_rejected": sorted(self._drive_rejected),
                    "rejected_alert": self._drive_reject_notified,
                    "no_response_alert": self._drive_noresp_notified,
                    # Booking hold: how far above comfort the hall is currently
                    # being held to pre-empt an evening dip (0 when not a running
                    # comfort booking, mild, or disabled).
                    "hold_margin": round(self._booking_hold_margin(ZONE_A), 3),
                },
                "coast": {
                    "enabled": self.switch_on("coast_when_free", default=False),
                    # Measured idle-room warming (°C/min); None while heaters
                    # are driving or too little idle history exists.
                    "passive_rise_c_per_min": self._passive_rise_rate(),
                    "samples": len(self._passive_rise),
                    "coasting": dict(self._coasting),
                },
                "fan": {
                    "on": self.fan_on,
                    "mode": self.fan_mode,
                    "direction": self.fan_direction,
                    "fault": self.fan_fault_effective,
                    "sensor_stale": self.fan_sensor_stale,
                    "overheated": self.fan_overheated,
                    "breeze_hot": self.fan_breeze_hot,
                    "mix": self.fan_mix,
                    "cooling_wanted": self._fan_cooling_wanted,
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
        setpoints, and one full reconcile). It does not touch boosts, a hall
        pause, manual holds or the latched fan fault — resetting sliders must not
        silently re-arm a faulted fan or resume heat someone paused.
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

    @callback
    def _handle_fire_event(self, event: Event) -> None:
        """Latch the fire hold on a panel fire (the alarm integration's event).

        The 230 V fan supply is hardware-cut on a fire output — the real safety —
        but HA does not otherwise know a fire happened, so it would re-arm the
        fans on the next power blip. Latching here forces EVERYTHING off (heating
        -> ice, water off, fans off) and, crucially, makes the wanted fan state
        `off`, so a Shelly reboot mid-fire re-establishes OFF, not the fans. The
        latch clears only on the deliberate `async_clear_fire_hold` (a human
        confirming it is safe). Rising-edge only: repeat fire events while already
        held are a no-op.
        """
        if event.data.get("event_type") not in FIRE_EVENT_TYPES:
            return
        if self._fire_hold:
            return
        self._fire_hold = True
        self.audit.record(
            "fire",
            self._now(),
            zone_name=event.data.get("zone_name"),
            description=event.data.get("description"),
        )
        self.async_request_reconcile()

    async def async_clear_fire_hold(self) -> None:
        """Clear the fire hold — the deliberate human "it is safe" gesture.

        Nothing auto-clears it: there is no clean "fire over" signal from the
        panel, and after a fire the whole hut should stay off until someone has
        checked. Only this (the *Clear fire hold* button) releases it.
        """
        if not self._fire_hold:
            return
        self._fire_hold = False
        self._fire_notified = False
        self.audit.record("fire_cleared", self._now())
        persistent_notification.async_dismiss(self.hass, NOTIFY_FIRE)
        await self.async_reconcile()

    async def _update_fire_alarm(self) -> None:
        """Surface the fire hold (rising edge): persistent + companion push."""
        if self._fire_hold and not self._fire_notified:
            self._fire_notified = True
            title = "🔥 Scout Hut – FIRE: everything held OFF"
            message = (
                "A fire was signalled by the alarm panel. Heating, the water "
                "heater and the fans are all held OFF and will NOT resume on "
                "their own — not even after a power cut. Once it is confirmed "
                "safe, press 'Clear fire hold' to return to normal."
            )
            persistent_notification.async_create(
                self.hass, message, title=title, notification_id=NOTIFY_FIRE
            )
            await self._push_companion(
                title, message, icon="mdi:fire", channel="Scout Hut Fire"
            )

    # ------------------------------------------------------------------
    # Desired-state computation
    # ------------------------------------------------------------------
    def _eco_keyword_active(self, zone: str) -> bool:
        title = self.cal_title.get(zone, "")
        return any(kw in title for kw in self.eco_keywords())

    def _cal_active(self, zone: str) -> bool:
        return self._is_on(self.config.get(ZONE_CALENDAR[zone])) or self.cal_window[zone]

    def _booking_target(self, zone: str) -> float:
        """The setpoint a booking / pre-heat is aiming for.

        Comfort, or the lower eco-low setpoint for an ECO-keyword event — the
        same target the reconciler will push and the pre-heat sizes for. This
        is the temperature the cold-booking lockout bypass judges the room
        against.
        """
        if self._eco_keyword_active(zone):
            return self.number("hall_eco_low_temp")
        return self._zone_target(zone)

    def _room_wants_heat(self, zone: str, target: float) -> bool:
        """True when an occupied / booked zone is genuinely below ``target`` and
        so wants heat — the single, season-independent heating gate.

        "Cold" is read from the room's own coldest heater probe (the far end of
        the 20 m hall) against ``target``, self-calibrating with no weather
        threshold to guess: a warm room never wants heat whatever the calendar
        says, so the fans are free to cool it, while a genuinely cold present or
        booked room always heats. Release hysteresis keyed off the applied preset
        (``COLD_BOOKING_RELEASE_BAND``) keeps the decision from flapping around
        the setpoint. A frozen Rointe reading is rejected (``stale_min``).

        An UNREADABLE room errs WARM (heat) — the heating fail-safe direction —
        but only after two cheaper answers are exhausted, so a transient Rointe
        drop-out cannot flip a genuinely warm hall to comfort (and reverse the
        cooling fans) on a hot afternoon (field 2026-08-11: a ~17 s hall-probe
        blip flipped ice→comfort→ice, one spurious fan reversal per blip):
          (A) hold the room's OWN last good reading through a brief blip
              (`ROOM_READING_GRACE_MIN`) — the most reliable "other reading",
              only seconds stale and safe in every season;
          (C) on a SUSTAINED loss, consult the independent ceiling (hall only)
              before erring warm. A warm ceiling only reliably implies a warm
              floor when stratification cannot have decoupled them — i.e. when it
              is also warm OUTSIDE (this bug's hot day), not a cold day with
              residual roof heat pooled over a cooling floor. So heat is withheld
              only when BOTH the ceiling and the outdoor sit at/above target;
              anything else falls through to err-warm.
        The Rointe still governs the actual firing against its own probe, so a
        room that is really warm will not fire anyway.
        """
        room = self._zone_room_temp(zone, coldest=True, stale_min=self._rointe_stale_min())
        now = self._now()
        if room is not None:
            self._last_room_temp[zone] = (now, room)
        else:
            last = self._last_room_temp.get(zone)
            if (
                last is not None
                and (now - last[0]).total_seconds() <= ROOM_READING_GRACE_MIN * 60
            ):
                room = last[1]  # (A) hold the recent reading through the blip
            else:
                # (C) sustained loss: independent evidence, or err warm.
                ceiling = self._ceiling_temp() if zone == ZONE_A else None
                outdoor = self._outdoor_temp()
                if (
                    ceiling is not None
                    and ceiling >= target
                    and outdoor is not None
                    and outdoor >= target
                ):
                    return False  # confirmed warm inside and out — no heat
                return True  # err warm — the heating fail-safe
        margin = (
            COLD_BOOKING_RELEASE_BAND
            if self.applied[zone] in (PRESET_COMFORT, PRESET_ECO)
            else 0.0
        )
        return room < target + margin

    def _update_passive_rise(self) -> None:
        """Accumulate idle-room readings for the coast predictor.

        Runs at the top of every reconcile, before the ladder reads the rate.
        The sample is the hall's coldest heater reading — the same far-end
        measure the pre-heat sizes its deficit against. It is recorded ONLY
        while the heaters are idle (`_heat_demand()` false): a rise measured
        while the radiators drive is their work, not free gain, and feeding
        that back into a heat-suppression decision is the exact trap to avoid.
        Any active demand, or a lost reading, clears the buffer — so the
        measured slope is always a clean idle-room climb, and the predictor
        cannot oscillate (applying heat wipes the evidence for withholding it).
        """
        now = self._now()
        room = self._zone_room_temp(ZONE_A, coldest=True)
        if room is None or self._heat_demand():
            self._passive_rise.clear()
            return
        self._passive_rise.append((now, room))
        cutoff = now - timedelta(minutes=PASSIVE_RISE_WINDOW_MIN)
        while self._passive_rise and self._passive_rise[0][0] < cutoff:
            self._passive_rise.popleft()

    def _passive_rise_rate(self) -> float | None:
        """The hall's measured idle-room warming in °C/min, or None.

        None until the buffer spans at least PASSIVE_RISE_MIN_SPAN_MIN of idle
        history (comfort-lean: too little data → no rate → the pre-heat heats).
        A simple first-to-last slope over the window; the readings are coarse
        and the window long, so a least-squares fit would add nothing.
        """
        if len(self._passive_rise) < 2:
            return None
        (t0, temp0), (t1, temp1) = self._passive_rise[0], self._passive_rise[-1]
        span_min = (t1 - t0).total_seconds() / 60
        if span_min < PASSIVE_RISE_MIN_SPAN_MIN:
            return None
        return (temp1 - temp0) / span_min

    def _preheat_gap_min(self, zone: str) -> float | None:
        """Minutes until the pre-heating event starts, from the last look-ahead.

        `_zone_preheat_minutes` stashes the gap each ~5-min calendar refresh
        while the event is upcoming (not yet running). Up to a few minutes
        stale, which is immaterial against a lead of an hour or more.
        """
        calc = self._last_lead_calc.get(zone)
        return calc.get("gap_min") if calc else None

    def _note_coasting(self, zone: str, active: bool) -> None:
        """Latch the coast decision and audit the moment it engages.

        The preset transition itself is already audited (comfort→eco, reason
        `preheat_coast`); this adds a `coast_decision` event carrying the
        prediction inputs on the engaging edge, so a later export can check
        whether the room actually arrived — the same tune-from-evidence pattern
        as `preheat_start`. Only the False→True edge is recorded; the resume is
        visible as the preset change back to comfort.
        """
        if active and not self._coasting[zone]:
            self.audit.record(
                "coast_decision",
                self._now(),
                zone=zone,
                indoor_coldest=self._zone_room_temp(zone, coldest=True),
                target=self._zone_target(zone),
                rise_rate_c_per_min=self._passive_rise_rate(),
                gap_min=self._preheat_gap_min(zone),
            )
        self._coasting[zone] = active

    def _should_coast(self, zone: str, target: float, gap_min: float | None) -> bool:
        """True when the hall can be held at eco: free gain covers the comfort.

        Gated on the `coast_when_free` switch (default off) and hall-only. Reads
        the measured idle-room rise rate and asks coast.will_coast_to_target
        whether, at that rate, the room reaches the comfort band by the deadline
        with a margin. ``gap_min`` is the minutes until the room is needed: the
        pre-heat lead for an upcoming event, or 0.0 for a running booking (whose
        deadline is now, reducing the test to "already in the band and rising").
        Comfort-lean throughout: no switch, no rate, or an unknown gap all fall
        through to heating.
        """
        if zone != ZONE_A or not self.switch_on("coast_when_free", default=False):
            return False
        return will_coast_to_target(
            indoor=self._zone_room_temp(
                ZONE_A, coldest=True, stale_min=self._rointe_stale_min()
            ),
            target=target,
            rise_rate=self._passive_rise_rate(),
            gap_min=gap_min,
        )

    def _reason(self, zone: str, reason: str, preset: str | None) -> str | None:
        """Stash why a zone's desired preset is what it is (for the audit)."""
        self._preset_reason[zone] = reason
        return preset

    def _desired_zone(self, zone: str) -> str | None:
        # Fire fallback beats everything, including automation-disabled and a
        # manual hold: on a signalled fire the whole hut goes to frost-protect ice
        # and stays there until a person clears the hold.
        if self._fire_hold:
            return self._reason(zone, "fire", PRESET_ICE)
        enabled_key = f"{zone}_automation_enabled"
        if not self.switch_on(enabled_key, default=True):
            return None
        if self.manual_hold[zone]:
            return None
        # Occupant "too warm" cutout (hall only): beats boost and bookings, but
        # still lands on ice, so frost protection holds. It only clears on a
        # deliberate action or a fresh session out of an idle gap.
        if zone == ZONE_A and self.hall_heating_paused:
            return self._reason(zone, "heating_paused", PRESET_ICE)
        if self.opening_ice[zone]:
            return self._reason(zone, "opening", PRESET_ICE)
        if self.boost_active(zone):
            return self._reason(zone, "boost", PRESET_COMFORT)

        cal_on = self._cal_active(zone)
        alarm_on = self._alarm_armed(self.config.get(ZONE_ALARM[zone]))
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

        # --- Booking / pre-heat ------------------------------------------------
        # A booking is occupancy we know about IN ADVANCE, so it gets the two
        # things bare presence cannot: a pre-heat lead (warm from minute one) and
        # persistence — it holds the target through the whole slot. Otherwise it
        # heats on exactly the same test as occupancy below: only when the room
        # is genuinely below the target the booking asked for
        # (`_room_wants_heat`, self-calibrating and season-independent). A warm
        # booking lands on ice, freeing the cooling fans if the hall is hot.
        if cal_on:
            base = PRESET_ECO if self._eco_keyword_active(zone) else PRESET_COMFORT
            event_running = self._is_on(self.config.get(ZONE_CALENDAR[zone]))
            # "Occupied" for a running booking counts positive presence signals the
            # PIR can't see, so a booking is not demoted to eco (`booking_quiet`)
            # while people are present but STILL — a sleepover. Recent hall motion,
            # the manual occupied-override switch, or a Night/Home alarm arm (people
            # sleeping inside) all hold comfort; a genuinely empty booking (no
            # motion, no override, not night-armed) still drops to eco.
            occupied = (
                self._motion_recent(area, timeout)
                or self.switch_on(f"{zone}_occupied_override")
                or self._alarm_present(self.config.get(ZONE_ALARM[zone]))
            )
            # A running comfort booking holds the room a little ABOVE comfort (the
            # booking hold) so a cooling evening cannot drop it below comfort while
            # the slow drive catches up. The gate engages at that raised target so
            # the hall does not ice while still above bare comfort; the margin is 0
            # for eco bookings, pre-heat (not yet running) and mild nights.
            booking_target = self._booking_target(zone) + self._booking_hold_margin(zone)
            if not self._room_wants_heat(zone, booking_target):
                # Already warm enough for what this booking asked — no heat, and
                # ice lets the cooling fans run if the room is genuinely hot.
                self._note_coasting(zone, False)
                return self._reason(zone, "booking_warm", PRESET_ICE)
            # "Will it get there / stay there on its own?" Hold at eco instead of
            # firing the radiators when free gain (sun on the roof, occupancy,
            # warm fabric) is doing the work — comfort kept, delivered free:
            #  - PRE-HEAT window (event not running): the room is measurably
            #    climbing and will reach the comfort band by event start with a
            #    margin (deadline = start). The deliberate exception to the
            #    "never demote a pre-heat" rule below.
            #  - RUNNING, OCCUPIED booking: the room is ALREADY in the band and
            #    still measurably rising, so free gain is holding comfort now
            #    (deadline = now, so the predictor reduces to in-band + rising —
            #    it can never withhold heat from an occupied room actually below
            #    comfort). The 2026-08-05 case: a booking whose floor climbed
            #    19.4 -> 20.0 with the heaters off, on occupancy + the fans.
            # Comfort-lean, hall-only, re-evaluated every tick so a fading gain
            # resumes heat immediately.
            if base == PRESET_COMFORT and (not event_running or occupied):
                gap = self._preheat_gap_min(zone) if not event_running else 0.0
                if self._should_coast(zone, self._zone_target(zone), gap):
                    self._note_coasting(zone, True)
                    reason = "preheat_coast" if not event_running else "booking_coast"
                    return self._reason(zone, reason, PRESET_ECO)
                self._note_coasting(zone, False)
            else:
                # Not a coast-eligible tick (unoccupied running booking, or an
                # eco-keyword pre-heat): clear any stale latch so the next
                # eligible session audits its engaging edge afresh.
                self._note_coasting(zone, False)
            # Drop an unoccupied booking to eco only once the event has actually
            # started (persistence stops at the empty room). During the pre-heat
            # window the room is empty by definition — demoting there would heat
            # toward eco while the optimum-start lead was sized to reach comfort,
            # so hirers would always arrive to a shortfall.
            if base == PRESET_COMFORT and event_running and not occupied:
                return self._reason(zone, "booking_quiet", PRESET_ECO)
            if base == PRESET_ECO:
                return self._reason(zone, "booking_eco", base)
            return self._reason(zone, "booking" if event_running else "preheat", base)

        # --- Occupancy ---------------------------------------------------------
        # Someone is in the zone right now. Heat toward the SAME comfort target a
        # booking would — occupancy and a booking are not different behaviours,
        # they only differ in foreknowledge and persistence. Presence is any of:
        # recent motion, the manual occupied-override switch, or a Night/Home
        # alarm arm — the last being a sleepover the PIR can't see when everyone
        # is still and asleep (the same positive-presence signal booking_quiet
        # trusts, now honoured WITHOUT a booking so a sleepover with nothing on
        # the calendar still heats instead of frost-protecting a room full of
        # sleepers). It heats only while presence is confirmed (no advance, no
        # hold once they leave) and only when the room is genuinely below comfort;
        # a warm occupied hall lands on ice so the cooling fans run.
        occupied_override = self.switch_on(f"{zone}_occupied_override")
        motion = self._motion_recent(area, timeout)
        alarm_present = self._alarm_present(self.config.get(ZONE_ALARM[zone]))
        if occupied_override or motion or alarm_present:
            if self._room_wants_heat(zone, self._zone_target(zone)):
                reason = (
                    "occupied_override"
                    if occupied_override
                    else "motion"
                    if motion
                    else "sleepover"
                )
                return self._reason(zone, reason, PRESET_COMFORT)
            return self._reason(zone, "occupied_warm", PRESET_ICE)
        if not self._motion_recent_any(timeout):
            return self._reason(zone, "building_empty", PRESET_ICE)
        # Zone quiet but someone is elsewhere in the building: rest at eco rather
        # than leaving a stale comfort preset running (e.g. the hall after a
        # booking ends while a cleaner is still in the kitchen).
        return self._reason(zone, "others_present", PRESET_ECO)

    def _desired_shared(self) -> str | None:
        if not self.config.get(CONF_SHARED_CLIMATES):
            return None
        if self._fire_hold:
            return self._reason("shared", "fire", PRESET_ICE)
        # The shared kitchen/toilets/stores heat toward `shared_comfort_temp`
        # (like the hall/office zones) when the block is genuinely in use — a
        # hall/office booking is running (people are in for a session and will
        # use the kitchen/toilets) OR there is motion in the shared PIRs
        # themselves (kitchen/gents/female). Gated by the shared zone being
        # genuinely cold (`_shared_wants_heat`); a warm shared zone rests at eco.
        # Bare motion only in the hall/office (nobody in the shared rooms) keeps
        # the lighter eco floor, so a cleaner in the office does not warm the
        # toilets to comfort. No seasonal gate: the season no longer blocks heat.
        if self.opening_ice["shared"]:
            return self._reason("shared", "opening", PRESET_ICE)
        if self.boost_active(ZONE_A) or self.boost_active(ZONE_B):
            return self._reason("shared", "boost", PRESET_COMFORT)
        if self._alarm_armed(self.config.get(CONF_ALARM_MAIN)) and self._alarm_armed(
            self.config.get(CONF_ALARM_OFFICE)
        ):
            return self._reason("shared", "alarm", PRESET_ICE)
        timeout = self.number("motion_timeout_minutes")
        cal_a = self._cal_active(ZONE_A)
        cal_b = self._cal_active(ZONE_B)
        booking = cal_a or cal_b
        # The shared block serves whoever is booked, so the WARMEST active booking
        # wins: a real comfort session (hall or office) heats the toilets/kitchen
        # to comfort, but when EVERY active booking is an eco-keyword (low-key
        # cleaning) one the shared follows the eco floor, matching the hall. This
        # stops a sal-vation session driving the toilets to full comfort — heat the
        # room did not need (it was already above eco) — whose demand then spun the
        # hall fans with nothing to reclaim (field 2026-08-21). A concurrent
        # non-eco office booking still wins, so an eco hall booking can never
        # downgrade the shared below what a real office session needs.
        eco_booking = booking and not (
            (cal_a and not self._eco_keyword_active(ZONE_A))
            or (cal_b and not self._eco_keyword_active(ZONE_B))
        )
        shared_motion = any(self._motion_recent(a, timeout) for a in WATER_MOTION_AREAS)
        if booking or shared_motion:
            if eco_booking:
                # Low-key session (the cleaner IS the motion): rest at eco, which
                # the Rointe idles at once warm enough, so no demand is created.
                return self._reason("shared", "booking_eco", PRESET_ECO)
            if self._shared_wants_heat():
                reason = "booking" if booking else "shared_motion"
                return self._reason("shared", reason, PRESET_COMFORT)
            return self._reason("shared", "shared_warm", PRESET_ECO)
        if self._motion_recent_any(timeout):
            return self._reason("shared", "motion", PRESET_ECO)
        return self._reason("shared", "building_empty", PRESET_ICE)

    def _shared_room_temp(self, stale_min: float | None = None) -> float | None:
        """Coldest reported room temperature around the water heater.

        Reads the shared-zone (kitchen/toilet) Rointe climates, ignoring any
        heater that is unavailable; the tank lives in that zone, so the coldest
        reading is the one that matters for frost. ``stale_min`` (see
        ``_zone_climate_temps``) drops a heater whose reading has frozen — passed
        on the warm-enough decision path (`_shared_wants_heat`), omitted for the
        frost/diagnostic reads where a stale value is harmless.
        """
        vals: list[float] = []
        for climate in self._as_list(self.config.get(CONF_SHARED_CLIMATES)):
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
        return min(vals) if vals else None

    def _shared_wants_heat(self) -> bool:
        """True when the shared zone is genuinely below its comfort target — the
        shared analog of `_room_wants_heat`.

        Reads the coldest shared heater probe (freshness-gated) against
        `shared_comfort_temp`, with the same `COLD_BOOKING_RELEASE_BAND` release
        hysteresis keyed off the applied preset. An unreadable shared zone errs
        WARM (heats), like the zone gate — the Rointe governs real firing against
        its own probe.
        """
        room = self._shared_room_temp(stale_min=self._rointe_stale_min())
        if room is None:
            return True
        margin = (
            COLD_BOOKING_RELEASE_BAND
            if self.applied["shared"] in (PRESET_COMFORT, PRESET_ECO)
            else 0.0
        )
        return room < self.number("shared_comfort_temp") + margin

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
        if self._fire_hold:
            return False  # fire: water heater off with everything else
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
        both_alarms = self._alarm_armed(
            self.config.get(CONF_ALARM_MAIN)
        ) and self._alarm_armed(self.config.get(CONF_ALARM_OFFICE))
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
            self._update_passive_rise()
            await self._reconcile_zones()
            await self._reconcile_hall_temps()
            self._update_warmup_learning()
            self._update_cooloff_learning()
            await self._update_opening_inferred_alarm()
            await self._update_fire_alarm()
            await self._reconcile_shared()
            await self._reconcile_drive()
            await self._reconcile_water()
            await self._reconcile_fans()
            self._note_fan_speed()
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

    def _hall_heaters_firing(self) -> int:
        """How many hall heaters report ``hvac_action == heating`` right now.

        Recorded in the trace so a later export can attribute a climb: a warm-up
        that reached target with ``hall_fire`` 0 was free gain (fans + occupancy
        + solar), not the radiators, while a non-zero count over the climb is the
        heaters doing the work. Floor temperature alone cannot tell them apart —
        which is why the 2026-08-27 climb could not be credited to the drive
        rather than the fans. NB ``hvac_action`` is two-valued here
        (heating/idle); it does NOT distinguish the Rointe's throttled
        *maintaining* half-power state — that needs the device's own
        ``heating_status`` sensor (Q17), which this count is not a substitute for.
        """
        n = 0
        for climate in self._as_list(self.config.get(ZONE_CLIMATES[ZONE_A])):
            st = self.hass.states.get(climate)
            if st is not None and st.attributes.get("hvac_action") == "heating":
                n += 1
        return n

    def _hall_drive_offset(self) -> float:
        """The largest overdrive the drive has wound onto a hall heater (°C above
        target), recorded so the trace shows how hard the drive was pushing
        through a climb — the signal for whether the setpoint drive, rather than
        the fabric, was ever the limiter. 0 when the drive is off or idle."""
        stair = self._drive_stair
        offsets = [
            stair.get(climate, 0.0)
            for climate in self._as_list(self.config.get(ZONE_CLIMATES[ZONE_A]))
        ]
        return round(max(offsets), 2) if offsets else 0.0

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
            fan_mode=self.fan_mode,
            # Hall occupancy alongside fans/mode makes empty-building winter fan
            # running measurable directly from the trace (the occupancy gate's
            # effect, and whether it needs revisiting).
            occupied=self._cooling_occupied(),
            demand=self.heat_demand,
            # Heater attribution + drive aggression, so a climb can be credited to
            # the radiators vs free gain and the drive's push read through it (the
            # 2026-08-27 gap: a warm-up reached target but the trace could not say
            # whether the drive or the fans got it there).
            hall_fire=self._hall_heaters_firing(),
            drive_off=self._hall_drive_offset(),
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
                # A hall booking ending is the deliberate boundary that lifts a
                # pause carried through the session: an adjacent next booking
                # then starts fresh (inheriting the still-warm room, so its
                # pre-heat is naturally reduced or skipped).
                if zone == ZONE_A:
                    self._clear_hall_pause("booking_end")
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
                # The fan tap the pre-heat's fan-assisted rate assumed, so a
                # shortfall can be read against a speed the occupants dropped.
                fan_w_last=self._fan_w_last_seen if zone == ZONE_A else None,
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

    def _hall_desired_setpoint(self) -> float:
        """The temperature the hall is currently being driven toward — the
        applied preset's setpoint.

        Used to gate destratification recirculation: harvesting stored ceiling
        heat only helps when the room is BELOW what is wanted, so the fans chase
        the same goal the heaters do. When there is no heating goal the applied
        preset is ice, which returns the anti-frost floor (7) — so a warm-enough
        or frozen hall (e.g. an eco-low booking whose room already sits above its
        low target) does not destratify unwanted heat onto its occupants. Because
        a booked zone only lands on ice once its room is already at/above the
        booking target, the applied setpoint is a faithful proxy for "what the
        occupants actually want".
        """
        applied = self.applied[ZONE_A]
        if applied == PRESET_COMFORT:
            # Chase the boosted target too, so the destrat fans keep delivering
            # heat to head height while a Boost is driving the room past comfort.
            return self._drive_comfort_target(ZONE_A)
        if applied == PRESET_ECO:
            return self._hall_eco_target(self._eco_keyword_active(ZONE_A))
        return ROINTE_ANTIFROST

    def _hall_eco_target(self, eco_low: bool) -> float:
        """The eco setpoint to push for the hall.

        Eco-keyword bookings get the low setpoint; every other eco path (an
        unoccupied running booking gone quiet, a coast hold, someone elsewhere
        in the building) keeps the ordinary eco number.
        """
        if eco_low:
            return self.number("hall_eco_low_temp")
        return self.number("hall_eco_temp")

    async def _reconcile_hall_temps(self) -> None:
        """Re-assert the hall comfort/eco setpoints when the intended value
        changes while the hall is in a heating preset — even without a preset
        transition.

        The per-preset push in `_async_set_preset` only fires on a preset
        CHANGE. So an eco-keyword booking that starts while the hall is already
        `eco` (e.g. a prior booking went quiet->eco, a coast hold, or someone
        elsewhere in the building left it at others-present eco) never gets
        eco-low (14) written and heats the whole session at 16. This backstop
        compares the intended (comfort, eco) pair
        against what was last pushed and re-pushes only on a genuine change, so
        it costs nothing per tick when nothing moved.
        """
        if self.applied[ZONE_A] not in (PRESET_COMFORT, PRESET_ECO):
            return
        eco_low = self._eco_keyword_active(ZONE_A)
        comfort_temp = self.number("hall_comfort_temp")
        eco_temp = self._hall_eco_target(eco_low)
        if self._hall_temps_pushed != (comfort_temp, eco_temp):
            await self._async_push_hall_temps(eco_low=eco_low)

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
                # A held zone never gets a fresh apply, so its expected stays
                # None and the normal (matches) clear path below is unreachable
                # — a hold left by the old setpoint-drift bug would deadlock
                # here forever (it did: v1.14.0 set a hold, the v1.14.1 restart
                # persisted it, expected reset to None, and it could not clear).
                # With driving on a setpoint-based hold is no longer a valid
                # signal, so clear it; the next reconcile then applies the real
                # preset and normal (preset-based) drift resumes.
                if self.manual_hold[zone] and self.switch_on(
                    "drive_to_target", default=True
                ):
                    self.manual_hold[zone] = False
                    persistent_notification.async_dismiss(
                        self.hass, NOTIFY_ZONE_HOLD[zone]
                    )
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
            elif expected == PRESET_COMFORT and self.switch_on(
                "drive_to_target", default=True
            ):
                # The drive loop OWNS the comfort setpoint and varies it every
                # tick, and the Rointe cloud lags our pushes — so the reported
                # setpoint cannot judge drift here and a comparison would
                # false-flag a manual change (it did, on the v1.14.0 startup). A
                # genuine manual override still shows as a preset_mode change
                # (handled above) or the user disabling automation. Treat as no
                # drift, which also clears any hold this bug already latched.
                matches = True
                detail = "driven"
            else:
                # The Rointe integration in the field accepts set_preset_mode
                # but reports preset_mode as null, so judge drift from the
                # reported SETPOINT instead: each preset implies a known
                # target temperature on a Rointe (anti-frost is fixed at 7).
                matches = self._setpoint_matches(zone, state, expected, climates[0])
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

    def _setpoint_matches(
        self, zone: str, state: Any, expected: str, climate: str | None = None
    ) -> bool | None:
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
        eco_temp = self._hall_eco_target(eco_low)
        # Record the intended pair even if a side is unmappable, so the
        # reconciler does not retry a push that has nowhere to land every tick.
        self._hall_temps_pushed = (comfort_temp, eco_temp)
        if not eco_numbers or not comfort_numbers:
            # The whole point of eco-low is to write 14 to the device; if the
            # Rointe comfort/eco number entities cannot be resolved (disabled,
            # renamed away from the "comfort"/"eco" substring, or on another
            # device) the write silently vanishes and the heater keeps its own
            # setpoint. In a project whose instrument is the audit trail, a
            # swallowed setpoint write must be visible.
            self.audit.record(
                "hall_temp_push_skipped",
                self._now(),
                comfort_found=bool(comfort_numbers),
                eco_found=bool(eco_numbers),
                eco_low=eco_low,
            )
        # This sets the BASE comfort setpoint. When the drive-to-target loop is
        # on it then refines each heater's number to target + trim on top (the
        # trim starts at zero on entry, so there is no dip); this base push
        # still runs so the comfort setpoint is always placed even if the drive
        # cannot resolve a per-heater number.
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
    # Drive to target (outer per-heater setpoint trim loop)
    #
    # The Rointe firmware drives its element to whatever setpoint we give it, on
    # its OWN probe, but settles a fraction under (and reports a modelled "full"
    # power the radiators do not match). Because the integration owns the
    # setpoint it pushes, it closes an outer loop on each heater's probe and
    # overdrives the setpoint until the probe actually reaches target — the pure
    # controller lives in drive.py; the safety policy lives here.
    # ------------------------------------------------------------------
    def _boosting(self, zone: str) -> bool:
        """Is this zone currently under a Boost? Shared follows either room's boost."""
        if zone == "shared":
            return self.boost_active(ZONE_A) or self.boost_active(ZONE_B)
        return self.boost_active(zone)

    def _booking_hold_margin(self, zone: str) -> float:
        """°C above comfort to hold the hall at during a running booking so it does
        not dip below comfort as the evening cools (see preheat.hold_margin).

        Hall only, comfort bookings only (an ECO-keyword booking targets eco-low,
        not comfort), and only once the event is actually running — the pre-heat
        owns the arrival, this owns holding the floor through the slot. Sized from
        the same learned cool-off and warm-up rates the pre-heat uses, so it is
        self-calibrating and self-zeroing (mild night / unlearned rates → 0). Not
        applied while the coast predictor is holding at eco on free gain (they are
        opposite decisions and cannot both be right)."""
        if zone != ZONE_A or self._eco_keyword_active(zone):
            return 0.0
        if not self._is_on(self.config.get(ZONE_CALENDAR[zone])):
            return 0.0
        if self._coasting.get(zone):
            return 0.0
        rate, _ = self._prediction_rate(zone)
        return hold_margin(
            comfort=self._zone_target(zone),
            outdoor=self._outdoor_temp(),
            cool_k=self.number(f"{zone}_heatloss_pct") / 100,
            warmup_rate=rate,
            cap=self.number("booking_hold_cap"),
        )

    def _drive_comfort_target(self, zone: str) -> float:
        """The temperature the drive aims the room at — comfort, plus any upward
        nudge from a Boost or a booking "hold".

        A Boost is an occupant saying "still too cold", so it aims ABOVE comfort
        (without it, boosting a room already at comfort would be a no-op). A
        booking hold anticipates a cooling evening, holding the hall a little
        above comfort so the slow drive does not undershoot. Both only ever aim
        warmer; take the larger (they do not stack) and clamp to the Rointe max.

        Quantised to the Rointe's 0.5 °C setpoint grid: the booking-hold margin is
        a continuous number (e.g. 0.72 °C), but the radiators only accept 0.5 steps
        — an off-grid target is silently rounded by the device (and would wobble
        the setpoint read-back self-check), so we round it here at the boundary."""
        base = self.number(DRIVE_COMFORT_TARGET_KEY[zone])
        bump = self.number("boost_offset") if self._boosting(zone) else 0.0
        bump = max(bump, self._booking_hold_margin(zone))
        target = min(base + bump, ROINTE_COMFORT_MAX)
        return round(target / DRIVE_STEP) * DRIVE_STEP

    def _drive_heatloss_frac(self, zone: str) -> float:
        # The shared zone has no learned heat-loss of its own; borrow the hall's
        # (the same leaky main-building fabric) so the feedforward still helps.
        key = "zone_a_heatloss_pct" if zone == "shared" else f"{zone}_heatloss_pct"
        return max(0.0, self.number(key) / 100.0)

    def _heater_comfort_number(self, climate: str) -> str | None:
        """The Rointe comfort-temperature number on this heater's own device.

        Discovered per device (so each heater is driven independently) and
        cached; an empty string is cached for a heater with no such entity so we
        do not re-scan the registry every tick.
        """
        if climate in self._drive_number:
            return self._drive_number[climate] or None
        found = ""
        registry = er.async_get(self.hass)
        entry = registry.async_get(climate)
        if entry is not None and entry.device_id is not None:
            for member in er.async_entries_for_device(
                registry, entry.device_id, include_disabled_entities=False
            ):
                if member.domain == "number" and "comfort" in member.entity_id.lower():
                    found = member.entity_id
                    break
        self._drive_number[climate] = found
        return found or None

    def _heater_probe(self, climate: str) -> float | None:
        """This heater's own current temperature, or None if unavailable/stale.

        Freshness matters: a frozen Rointe reading looks alive but must not keep
        driving, so a report older than DRIVE_PROBE_STALE_MINUTES counts as lost.
        """
        st = self.hass.states.get(climate)
        if st is None or st.state in ("unavailable", "unknown"):
            return None
        ts = getattr(st, "last_reported", None) or st.last_updated
        if (dt_util.utcnow() - ts).total_seconds() > DRIVE_PROBE_STALE_MINUTES * 60:
            return None
        temp = st.attributes.get("current_temperature")
        try:
            return float(temp) if temp is not None else None
        except (TypeError, ValueError):
            return None

    async def _drive_push(
        self,
        climate: str,
        number: str,
        value: float,
        reassert: bool = False,
        force: bool = False,
    ) -> None:
        """Write a heater's comfort setpoint, only when it actually changes.

        ``reassert``: after writing the number, re-apply the comfort preset to
        the heater. A Rointe only adopts a changed comfort setpoint when the
        comfort preset is (re-)selected — writing the number alone leaves the
        live target unchanged (the existing slider-change path does both, see
        ``async_hall_temps_changed``). The drive must do the same or its boost
        never reaches the radiator. Only used on the driving (comfort) path; the
        withdrawal path just stores the plain target for the next real apply.

        ``force``: push (and re-assert, and restamp the settle clock) even when
        the number is unchanged. Used when a heater (re-)enters the driven state:
        its live setpoint may have been on ice while the withdrawal left the
        comfort *number* already at this value, so the reassert is what actually
        moves the radiator, and the fresh stamp restarts the read-back window.
        """
        if self._drive_pushed.get(climate) == value and not force:
            return
        self._drive_pushed[climate] = value
        # Stamp the push so the read-back self-check waits a full settle window
        # before judging whether the device adopted this new value (Q20).
        self._drive_pushed_at[climate] = self._now()
        await self.hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": number, "value": value},
            blocking=True,
        )
        if reassert:
            await self.hass.services.async_call(
                "climate",
                "set_preset_mode",
                {"entity_id": climate, "preset_mode": PRESET_COMFORT},
                blocking=False,
            )

    async def _reconcile_drive(self) -> None:
        """Drive each comfort heater's setpoint until its own probe reaches target.

        Runs after the presets are applied. For every zone currently in the
        COMFORT preset each heater gets a per-heater staircase trim on top of the
        zone's comfort target (pure maths in drive.py); the loop only ever drives
        HARDER than the owner's setpoint. Everything else is withdrawn to the
        plain target so nothing is left overdriven. Safety net: a stale or insane
        probe withdraws that heater; the per-heater cap bounds the worst case;
        sustained cap-pinning is surfaced.
        """
        if not self.switch_on("drive_to_target", default=True):
            # Disabled: withdraw once (restore plain targets), then hands off so
            # office/shared setpoints go back to being device-managed.
            if self._drive_pushed:
                await self.async_drive_reset()
            return

        now = self._now()
        outdoor = self._outdoor_temp()
        offset = self.number("drive_max_offset")
        any_capped_short = False
        for zone, cfg_key in DRIVE_ZONE_CLIMATES.items():
            climates = self._as_list(self.config.get(cfg_key))
            if not climates:
                continue
            # Hands off a zone the user has taken manual control of, or whose
            # automation is disabled: driving (or withdrawing) its setpoint
            # would fight a deliberate manual change. Leave whatever is there.
            if zone in (ZONE_A, ZONE_B) and (
                not self.switch_on(f"{zone}_automation_enabled", default=True)
                or self.manual_hold.get(zone)
            ):
                continue
            comfort = self.applied.get(zone) == PRESET_COMFORT
            target = self._drive_comfort_target(zone)
            cap = min(target + offset, ROINTE_COMFORT_MAX)
            loss = self._drive_heatloss_frac(zone)
            probes = {c: self._heater_probe(c) for c in climates}
            readable = sorted(v for v in probes.values() if v is not None)
            median = readable[len(readable) // 2] if readable else None
            for climate in climates:
                number = self._heater_comfort_number(climate)
                if number is None:
                    continue
                probe = probes[climate]
                sane = probe is not None and (
                    median is None or probe >= median - DRIVE_PROBE_SANE_BELOW
                )
                if not comfort or not sane:
                    # Fail-safe withdrawal: not being heated, or the probe is
                    # lost/glitched — restore the plain target, never leave it
                    # boosted on a reading we cannot trust.
                    self._drive_stair[climate] = 0.0
                    self._drive_cap_since[climate] = None
                    self._drive_rejected.discard(climate)
                    self._drive_driven.discard(climate)
                    await self._drive_push(climate, number, target)
                    continue
                since = self._drive_minutes_since_step(climate, now)
                pushed, stair, evaluated = update_drive(
                    target, probe, outdoor, loss, cap,
                    self._drive_stair.get(climate, 0.0), since,
                )
                self._drive_stair[climate] = stair
                if evaluated:
                    self._drive_step_at[climate] = now
                # Entering the driven state (a fresh comfort episode): re-assert
                # and restart the read-back window even if the comfort number is
                # unchanged from a prior withdrawal, so the read-back does not
                # judge a heater whose live setpoint was on ice moments ago.
                entering = climate not in self._drive_driven
                self._drive_driven.add(climate)
                await self._drive_push(
                    climate, number, pushed, reassert=True, force=entering
                )
                self._check_setpoint_readback(climate, pushed, probe, now)
                # Cap-pinned watch: at the cap AND still a full step short.
                if pushed >= cap - 1e-9 and (target - probe) >= DRIVE_STEP:
                    if self._drive_cap_since.get(climate) is None:
                        self._drive_cap_since[climate] = now
                    elif now - self._drive_cap_since[climate] >= timedelta(
                        minutes=DRIVE_CAP_ALARM_MINUTES
                    ):
                        any_capped_short = True
                else:
                    self._drive_cap_since[climate] = None
        self._update_drive_alarm(any_capped_short)
        # Self-validation (always on — was the `drive_self_check` switch): a
        # notification-only watch on whether our commands are landing.
        self._update_drive_reject_alarm()
        self._update_drive_no_response(now)

    def _drive_minutes_since_step(self, climate: str, now: datetime) -> float:
        at = self._drive_step_at.get(climate)
        if at is None:
            return DRIVE_STEP_INTERVAL_MIN  # allow the first step immediately
        return (now - at).total_seconds() / 60

    def _update_drive_alarm(self, capped_short: bool) -> None:
        if capped_short and not self._drive_notified:
            self._drive_notified = True
            self.audit.record("drive_capped", self._now())
            persistent_notification.async_create(
                self.hass,
                (
                    "A heater has been driven to its maximum setpoint for over "
                    f"{DRIVE_CAP_ALARM_MINUTES:.0f} minutes and still has not "
                    "reached target. That is either a genuine capacity limit "
                    "(the room needs more heat than the radiators can give) or a "
                    "stuck temperature sensor — worth a look."
                ),
                title="🏕 Scout Hut – Heater can't reach target",
                notification_id=NOTIFY_DRIVE_CAPPED,
            )
        elif not capped_short and self._drive_notified:
            self._drive_notified = False
            persistent_notification.async_dismiss(self.hass, NOTIFY_DRIVE_CAPPED)

    def _heater_setpoint(self, climate: str) -> float | None:
        """The heater's OWN reported active setpoint (its `temperature` attr).

        This is what the device says its target is — an independent read of
        whether our pushed setpoint was actually adopted. Unreadable → None
        (the read-back check then abstains rather than false-flag).
        """
        st = self.hass.states.get(climate)
        if st is None or st.state in ("unavailable", "unknown"):
            return None
        temp = st.attributes.get("temperature")
        try:
            return float(temp) if temp is not None else None
        except (TypeError, ValueError):
            return None

    def _comfort_number_state(self, climate: str) -> dict[str, Any] | None:
        """Diagnostic snapshot of the comfort-temperature NUMBER the drive writes.

        Splits a "setpoint not landing" three ways: `value` is what the number
        entity currently holds (did our write take?), `max` is its allowed ceiling
        (did the device clamp us below the target?), against the climate's live
        `setpoint` reported alongside it (did the number propagate to the target?).
        """
        number = self._heater_comfort_number(climate)
        if number is None:
            return None
        st = self.hass.states.get(number)
        if st is None:
            return {"entity": number, "value": None, "min": None, "max": None}
        try:
            value = float(st.state)
        except (TypeError, ValueError):
            value = None
        return {
            "entity": number,
            "value": value,
            "min": st.attributes.get("min"),
            "max": st.attributes.get("max"),
        }

    def _within_startup_grace(self, now: datetime) -> bool:
        """True while too soon after (re)start for the drive self-checks to judge.

        The Rointe cloud is much slower to reflect a pushed setpoint just after a
        restart than in steady state, so the settle window alone false-flags there
        (2026-08-07 export: all four hall heaters flagged ~10 min post-restart,
        then matched once the cloud caught up)."""
        return (
            self._started_at is None
            or (now - self._started_at).total_seconds() < DRIVE_STARTUP_GRACE_MINUTES * 60
        )

    def _check_setpoint_readback(
        self, climate: str, pushed: float, probe: float | None, now: datetime
    ) -> None:
        """Track whether a driven heater has adopted the setpoint we pushed (Q20a).

        Judged only after the push has had a full settle window to round-trip
        through the Rointe cloud, AND once past the post-restart startup grace
        (the cloud lags much longer just after boot); before either, or when the
        setpoint is unreadable, the check abstains (drops the heater from the
        rejected set) so ordinary lag never false-flags.

        A divergence once settled is only genuine when the heater is BOTH short of
        target AND idle. The phantom-push failure (v1.14.2) is "the command never
        reached the radiator", whose signature is a heater sitting idle at a
        stale-low setpoint *while the room is still cold*. Two independent proofs
        that the command DID land, either of which clears the flag:

          * the heater's own probe has reached the pushed target (``probe >=
            pushed - tol``): it is idle because it is SATISFIED, not because it
            rejected us — reaching the target could only happen if the setpoint was
            adopted (2026-08-09 export: three hall heaters flagged while idle at
            the 20.0 target the room had reached, their live setpoint merely
            lagging through the cloud — the action gate can't catch a *satisfied*
            heater because a satisfied heater is idle, not heating);
          * the heater reports ``hvac_action == heating``: it is demonstrably
            working toward target, its live setpoint just lagging our push by a
            quantum while the drive staircases upward (2026-08-08 export: all four
            hall heaters flagged mid-climb, live setpoint one 0.5 step behind).

        Only a heater that is short of the pushed target AND has gone idle AND is
        not reporting our setpoint is genuinely not accepting it.
        """
        at = self._drive_pushed_at.get(climate)
        if (
            at is None
            or self._within_startup_grace(now)
            or (now - at).total_seconds() < DRIVE_SETTLE_MINUTES * 60
        ):
            self._drive_rejected.discard(climate)
            return
        reported = self._heater_setpoint(climate)
        if reported is None or abs(reported - pushed) <= DRIVE_SETPOINT_TOL:
            self._drive_rejected.discard(climate)
            return
        # Reached the pushed target -> idle because satisfied, and the room could
        # not have got there unless the setpoint landed. Not the fault this is for.
        if probe is not None and probe >= pushed - DRIVE_SETPOINT_TOL:
            self._drive_rejected.discard(climate)
            return
        # Short of the pushed setpoint — but a heater actively heating has clearly
        # accepted the command (it is producing heat toward target); only a heater
        # that has gone IDLE while still short is genuinely not accepting it.
        st = self.hass.states.get(climate)
        action = st.attributes.get("hvac_action") if st else None
        if action == "heating":
            self._drive_rejected.discard(climate)
        else:
            self._drive_rejected.add(climate)

    def _update_drive_reject_alarm(self) -> None:
        rejected = bool(self._drive_rejected)
        if rejected and not self._drive_reject_notified:
            self._drive_reject_notified = True
            self.audit.record(
                "drive_setpoint_rejected", self._now(), heaters=sorted(self._drive_rejected)
            )
            persistent_notification.async_create(
                self.hass,
                (
                    "The heating is driving a heater but the heater's own "
                    "reported setpoint is not matching the value being sent, "
                    f"even after {DRIVE_SETTLE_MINUTES:.0f} minutes. The command "
                    "may not be reaching the radiator (a cloud/integration "
                    "issue) — the room could stay cold while the app looks "
                    "correct. A reload of the Rointe integration often clears it."
                ),
                title="🏕 Scout Hut – Heater not accepting its setpoint",
                notification_id=NOTIFY_DRIVE_REJECTED,
            )
        elif not rejected and self._drive_reject_notified:
            self._drive_reject_notified = False
            persistent_notification.async_dismiss(self.hass, NOTIFY_DRIVE_REJECTED)

    def _update_drive_no_response(self, now: datetime) -> None:
        """Independent ceiling witness (Q20b): heat requested, nothing responds.

        While the hall is in comfort and its coldest probe is still short of
        target, the room SHOULD be warming somewhere. The ceiling thermometer is
        an independent instrument: if, over a long window, NEITHER the floor NOR
        the ceiling rises, the requested heat is reaching nothing anywhere — a
        dead chain (phantom push, total outage), which a capacity wall is not (a
        capacity wall still warms the ceiling via stratification). Needs both the
        floor and the ceiling readable, or the witness is not independent and it
        abstains.
        """
        hall_comfort = self.applied.get(ZONE_A) == PRESET_COMFORT
        floor = self._zone_room_temp(ZONE_A, coldest=True, stale_min=self._rointe_stale_min())
        ceiling = self._ceiling_temp()
        target = self._drive_comfort_target(ZONE_A)
        short = floor is not None and floor < target - DRIVE_STEP
        if self._within_startup_grace(now) or not (hall_comfort and short) or floor is None or ceiling is None:
            self._drive_response_ref = None
            if self._drive_noresp_notified:
                self._drive_noresp_notified = False
                persistent_notification.async_dismiss(self.hass, NOTIFY_DRIVE_NO_RESPONSE)
            return
        ref = self._drive_response_ref
        if ref is None:
            self._drive_response_ref = (now, floor, ceiling)
            return
        ref_at, ref_floor, ref_ceiling = ref
        moved = (floor - ref_floor) >= DRIVE_NO_RESPONSE_EPS or (
            ceiling - ref_ceiling
        ) >= DRIVE_NO_RESPONSE_EPS
        if moved:
            # Something is responding — reset the window from here.
            self._drive_response_ref = (now, floor, ceiling)
            if self._drive_noresp_notified:
                self._drive_noresp_notified = False
                persistent_notification.async_dismiss(self.hass, NOTIFY_DRIVE_NO_RESPONSE)
            return
        if (now - ref_at) >= timedelta(minutes=DRIVE_NO_RESPONSE_MINUTES) and not (
            self._drive_noresp_notified
        ):
            self._drive_noresp_notified = True
            self.audit.record(
                "drive_no_response",
                self._now(),
                floor=floor,
                ceiling=ceiling,
                target=target,
                minutes=DRIVE_NO_RESPONSE_MINUTES,
            )
            persistent_notification.async_create(
                self.hass,
                (
                    "The hall is calling for heat and short of target, but over "
                    f"the last {DRIVE_NO_RESPONSE_MINUTES:.0f} minutes neither the "
                    "floor nor the ceiling has warmed at all. The heaters may not "
                    "be producing heat (a cloud dropout, lost power, or a command "
                    "not landing) — the hut is not warming. Worth checking the "
                    "Rointe integration and the heaters."
                ),
                title="🏕 Scout Hut – Heat requested but nothing is warming",
                notification_id=NOTIFY_DRIVE_NO_RESPONSE,
            )

    async def async_drive_reset(self) -> None:
        """Last will & testament: push every heater we may have overdriven back
        to its plain target, so a shutdown, reload or disable never leaves a
        setpoint sitting boosted (an unwanted warm hut with nothing to pull it
        back). Called on unload, and on startup before the first reconcile so a
        crash-left overdrive is undone within seconds of the next boot.
        """
        for zone, cfg_key in DRIVE_ZONE_CLIMATES.items():
            # Restore the owner's PLAIN comfort setpoint, never a transient Boost
            # or booking-hold bump — the last will exists to undo overdrive.
            target = self.number(DRIVE_COMFORT_TARGET_KEY[zone])
            for climate in self._as_list(self.config.get(cfg_key)):
                number = self._heater_comfort_number(climate)
                if number is None:
                    continue
                await self.hass.services.async_call(
                    "number",
                    "set_value",
                    {"entity_id": number, "value": target},
                    blocking=True,
                )
        self._drive_stair.clear()
        self._drive_step_at.clear()
        self._drive_pushed.clear()
        self._drive_pushed_at.clear()
        self._drive_driven.clear()
        self._drive_cap_since.clear()
        self._drive_rejected.clear()
        self._drive_response_ref = None
        if self._drive_notified:
            self._drive_notified = False
            persistent_notification.async_dismiss(self.hass, NOTIFY_DRIVE_CAPPED)
        if self._drive_reject_notified:
            self._drive_reject_notified = False
            persistent_notification.async_dismiss(self.hass, NOTIFY_DRIVE_REJECTED)
        if self._drive_noresp_notified:
            self._drive_noresp_notified = False
            persistent_notification.async_dismiss(self.hass, NOTIFY_DRIVE_NO_RESPONSE)

    # ------------------------------------------------------------------
    # Destratification / cooling fans
    #
    # Home Assistant only decides when the fans are wanted and in which
    # direction. The Shelly Pro 2PM script owns all timing and safety (the
    # coast-down dwell — the heavy blades take ~5 min to stop before the Finder
    # lets the other winding energise — the coil verification, stall / low-tap
    # protection and the latched fault). We never reproduce any of that here.
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
        # A heater on the ice (7 °C anti-frost) preset cannot be producing useful
        # heat — its setpoint sits below any occupied room — so a nonzero
        # effective-power reading there is the Rointe's MODELLED power (Q17), not
        # real demand. If no zone is being driven to heat, there is no demand
        # however the (often modelled) sensors read. Without this, a phantom
        # reading on a frost-protected heater asserts demand and, coinciding with
        # a brief ceiling-sensor `dt None`, flips the summer cooling fans to
        # reverse for a full reversal — the wrong direction on a hot afternoon
        # (field 2026-08-11: 4 spurious reversals, all heaters idle on ice).
        if not any(
            self.applied[z] in (PRESET_COMFORT, PRESET_ECO)
            for z in (ZONE_A, ZONE_B, "shared")
        ):
            return False
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

    @callback
    def _note_fan_master_state(self, state: str | None) -> None:
        """Record a fan-master state seen between reconcile ticks.

        The master can reboot (wall switch, power blip) faster than the 30 s
        reconcile poll: the Shelly goes unavailable then defaults its output
        off in ~1 s (field-observed 2026-08-08: `unavailable` at 00:32:08,
        `off` at 00:32:09), so a poll tick lands after the blip and sees only
        a straight available->off — which would latch a false `master_off`
        fault. The state-change event fires for the transient unavailability
        even when no reconcile coincides with it, so recording it here lets the
        next reconcile recognise the reboot and re-command instead of latching.
        Mirrors the `not master_known` branch in `_fan_fault`, which catches the
        same reboot when a poll *does* happen to see the unavailable state.
        """
        if state in ("unavailable", "unknown"):
            self._fan_master_seen_unavailable = True

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
        unavailability is caught either by a poll landing on it (the
        `not master_known` branch here) or, for a reboot too brief for a poll,
        by `_note_fan_master_state` off the state-change event. The inferred
        latch never auto-rearms; it clears only via async_fan_rearm.
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

    def _fan_cooling_regime(self, warm: bool | None, heating: bool) -> bool:
        """Whether the fans should run the COOLING (forward) regime this tick.

        Fully automatic from live room state — no toggle, no season:
          * active hall heating -> False (destratify: never wind-chill the people
            being warmed; the caller passes `heating`);
          * a genuinely warm (`warm`, head-height above `cooling_temp_high` with
            hysteresis) hall that is NOT being heated -> True (a cooling breeze
            for the people who are hot);
          * a cool hall, or an unknown reading -> False (destratify / off; a warm
            reading is REQUIRED, so unknown warmth never blows a draught on
            assumption).
        The season no longer enters into it: a warm hall gets a breeze whatever
        the calendar says, a cool one destratifies. (The seasonal lockout still
        governs whether expensive HEAT runs in summer — a separate concern — but
        it no longer steers the fans.)
        """
        return bool(warm) and not heating

    def _reset_fan_flags(self) -> None:
        """Clear every fan condition flag before a fans-off early-out, so a stale
        pre-condition value cannot keep feeding fail_safe_off or the diagnostics
        while the fans are held off (disabled / fault-latched / hall paused)."""
        self.fan_sensor_stale = False
        self.fan_dt = None
        self.fan_overheated = False
        self.fan_breeze_hot = False
        self._breeze_latch = False
        self.fan_mix = None
        self._fan_occupied = None
        self._fan_warm = None
        self._fan_cooling_wanted = False

    def _fan_target(self) -> tuple[bool, str | None, str]:
        """Resolve the desired fan state with fail-safe precedence on top."""
        if self._fire_hold:
            # Fire: fans OFF and the WANTED state is off, so a Shelly reboot
            # (power restored mid-fire) re-establishes off, never the fans.
            self._reset_fan_flags()
            return False, None, "off"
        if not self.switch_on("fans_enabled", default=True):
            self._reset_fan_flags()
            return False, None, "off"
        if self._fan_fault():
            self._reset_fan_flags()
            return False, None, "off"
        # A hall "too warm" pause is handled downstream by `allow_destrat` (the
        # reverse regime is suppressed, the cooling breeze is left running) —
        # keyed off live state, not the season, so a warm paused hall still gets
        # its breeze whatever the calendar says.

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

        # Head-height comfort estimate (0.75 x floor + 0.25 x ceiling): the air
        # a standing/seated occupant actually feels, part-way up the room. The
        # summer start trigger, the overheat cutoff and the hot-breeze guard all
        # read THIS single number, so the fans start, stop and hold on the same
        # basis. Needs both readings; the trigger falls back to the bare floor
        # when only the ceiling is missing (never the reverse — no floor means
        # "unknown", as before).
        self.fan_mix = None if (ct is None or ft is None) else 0.75 * ft + 0.25 * ct

        # Summer breeze judges warmth at head height, not at the floor sensor.
        # That sensor sits low on the wall and, on a still hot day under a hot
        # ceiling, reads cooler than the room a person is standing in — so
        # anchoring the trigger to it left occupants sweating just below the
        # line (observed 2026-07-12: floor 22.4 < 23 while head-height was 24.1,
        # fans stayed off). The overheat cutoff rides the same estimate: once
        # the air a fan would deliver hits skin temperature a breeze heats
        # people, whatever the floor lags at.
        comfort = self.fan_mix if self.fan_mix is not None else ft
        # Warm-enough-to-cool, with hysteresis on the DIRECTION boundary: enter
        # cooling above `cooling_temp_high`, but once cooling has started stay
        # cooling until the room drops COOLING_DIRECTION_HYST below it. Keyed off
        # the previous tick's mode (`fan_mode`), this stops a hall hovering at
        # the threshold from flapping the heavy fans forward<->reverse — the
        # stability the old season gate gave for free, now from the thermometer.
        high = self.number("cooling_temp_high")
        if comfort is None:
            warm: bool | None = None
        elif self.fan_mode == "summer":
            warm = comfort > high - COOLING_DIRECTION_HYST
        else:
            warm = comfort > high
        overheated = comfort is not None and comfort >= FAN_COOLING_MAX_TEMP
        self.fan_overheated = overheated
        self._fan_warm = warm

        # Hot-breeze guard: once that mixed air reaches the tunable ceiling a
        # breeze gives diminishing-to-negative benefit — hold the summer fans
        # and ask for the doors open instead. Releases 1 °C below so a value
        # hovering at the line cannot flap the fans; a lost reading leaves the
        # latch as-is (it clears only on a real drop below the release band).
        if self.fan_mix is not None:
            max_mix = self.number("cooling_mix_max_temp")
            if self.fan_mix >= max_mix:
                self._breeze_latch = True
            elif self.fan_mix <= max_mix - 1.0:
                self._breeze_latch = False

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
        # Recirculate residual / leaked ceiling heat only toward the temperature
        # the hall is actually being driven to — the applied setpoint — not a
        # fixed cap. Destratifying stored heat onto occupants only helps when the
        # room is BELOW what is wanted; so the fans chase the same goal the
        # heaters do, and harvest nothing when there is no goal. This is what
        # stops an eco-low booking whose room already sits above its low target
        # (applied ice) from having unwanted ceiling heat pushed down onto people
        # who asked for it cool. `fan_recirc_max_floor_temp` stays as an absolute
        # upper ceiling.
        recirc_target = min(
            self._hall_desired_setpoint(), self.number("fan_recirc_max_floor_temp")
        )
        recirc_ok = ft is not None and ft < recirc_target
        occupied = self._cooling_occupied()
        self._fan_occupied = occupied
        demand = self._heat_demand()
        self.heat_demand = demand  # building-wide, kept for diagnostics
        currently_winter = bool(self.fan_on) and self.fan_mode == "winter"
        # The hall is being heated (a boost or booking has set comfort/eco): a
        # forward cooling draught would chill the people we are warming, so this
        # forces the reverse/destrat regime even under the summer lockout. Keyed
        # off the applied preset, not demand, so the fan direction cannot flap as
        # the radiator thermostat cycles.
        heating = self.applied[ZONE_A] in (PRESET_COMFORT, PRESET_ECO)
        # Hall destrat runs on demand only when the HALL ITSELF is being heated —
        # never because another zone (office/shared) is making heat. `_heat_demand`
        # is building-wide (it catches office/shared heat leaking INTO the hall),
        # but when the hall is warm and on ice there is nothing to reclaim, so
        # spinning its fans on a neighbour's demand is pure cost — the Q15 leak
        # (field 2026-08-21: a sal-vation eco session drove the shared to comfort,
        # whose demand ran the hall fans while the hall sat idle on ice). Gating on
        # `heating` ties the hall fans to the hall's own heating, and the recirc
        # path already covers "the hall wants more heat delivered".
        hall_demand = demand and heating
        cooling_wanted = self._fan_cooling_regime(warm, heating)
        self._fan_cooling_wanted = cooling_wanted

        return fan_decision(
            summer=cooling_wanted,
            occupied=occupied,
            warm=warm,
            # The breeze guard holds the summer fans exactly like the hard
            # overheat cutoff does (the flag only gates the summer branch);
            # the notifications stay distinct.
            overheated=overheated or self.fan_breeze_hot,
            dt=dt,
            dt_on=self.number("fan_dt_on"),
            dt_off=self.number("fan_dt_off"),
            demand=hall_demand,
            recirc_ok=recirc_ok,
            # Always require occupancy for the no-demand recirc path (was the
            # `winter_fans_need_occupancy` switch): the field cool-off samples
            # settled that empty-hut fan-mixing buys no retention (~150 W for
            # nothing), so running on ambient stratification alone is never
            # wanted. Active heat demand still runs the fans regardless.
            recirc_needs_occupancy=True,
            heating=heating,
            currently_winter=currently_winter,
            run_on_loss=self.switch_on("fans_run_on_sensor_loss", default=True),
            # A hall "too warm" pause suppresses the reverse/destrat regime (no
            # roof heat pushed down onto the too-warm occupant); the forward
            # cooling breeze still runs if the room is warm and occupied.
            allow_destrat=not self.hall_heating_paused,
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
        if self.fan_overheated and not prev_hot and self._fan_cooling_wanted:
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
        if self.fan_breeze_hot and not prev_breeze and self._fan_cooling_wanted:
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
