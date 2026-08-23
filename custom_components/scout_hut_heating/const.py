"""Constants for the Scout Hut Heating integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "scout_hut_heating"

# How often the reconciler re-evaluates every zone. The original package used
# a mix of 1 and 5 minute time_pattern triggers; a single 30 second tick keeps
# behaviour responsive while remaining light.
RECONCILE_INTERVAL = timedelta(seconds=30)

# Delay after Home Assistant start before the first reconcile, so the Rointe,
# calendar and weather integrations have time to load (mirrors the original
# startup automations that waited 30 seconds).
STARTUP_DELAY = timedelta(seconds=30)

# Rointe / climate preset names used throughout.
PRESET_COMFORT = "comfort"
PRESET_ECO = "eco"
PRESET_ICE = "ice"

# ---------------------------------------------------------------------------
# Water heater safeguards (Hyco Speedflow 15 L point-of-use unit)
# ---------------------------------------------------------------------------
# The Speedflow's built-in frost protection only works while it is powered, and
# the controller keeps it switched off most of the time. Power it whenever the
# shared zone (kitchen/toilets, where the tank lives) nears freezing, releasing
# once the room recovers.
WATER_FROST_ON_TEMP = 3.0  # °C — coldest shared-zone room at/below this: power on
WATER_FROST_OFF_TEMP = 5.0  # °C — release once it recovers to this

# Stored-water hygiene: if the tank has gone this long without being powered,
# run it once so the full 15 L reaches thermostat temperature. A full reheat
# from cold winter mains takes ~30 min at 2 kW; 45 min gives margin, and the
# tank's own thermostat caps the temperature so a generous window is harmless.
WATER_HYGIENE_INTERVAL = timedelta(days=7)
WATER_HYGIENE_MINUTES = 45

# ---------------------------------------------------------------------------
# Config entry keys (entity mappings collected by the config flow)
# ---------------------------------------------------------------------------
# Climate zones
CONF_HALL_CLIMATES = "hall_climates"
CONF_OFFICE_CLIMATES = "office_climates"
CONF_SHARED_CLIMATES = "shared_climates"

# Rointe number entities used to push comfort / eco target temperatures onto
# the hall heaters before a preset is applied.
CONF_HALL_COMFORT_NUMBERS = "hall_comfort_numbers"
CONF_HALL_ECO_NUMBERS = "hall_eco_numbers"

# Motion sensors (binary_sensor or input_boolean)
CONF_MOTION_HALL = "motion_hall"
CONF_MOTION_OFFICE = "motion_office"
CONF_MOTION_KITCHEN = "motion_kitchen"
CONF_MOTION_GENTS = "motion_gents"
CONF_MOTION_FEMALE = "motion_female"

# Opening (contact) sensors
CONF_ZONE_A_DOORS = "zone_a_doors"
CONF_ZONE_A_WINDOWS = "zone_a_windows"
CONF_ZONE_B_DOORS = "zone_b_doors"
CONF_ZONE_B_WINDOWS = "zone_b_windows"
CONF_SHARED_WINDOWS = "shared_windows"
CONF_INTERNAL_DOOR = "internal_door"

# Calendars, weather and alarm
CONF_CALENDAR_HALL = "calendar_hall"
CONF_CALENDAR_OFFICE = "calendar_office"
CONF_WEATHER = "weather_entity"
CONF_REALFEEL = "realfeel_sensor"
CONF_ALARM_MAIN = "alarm_main"
CONF_ALARM_OFFICE = "alarm_office"

# Water heater switch
CONF_WATER_SWITCH = "water_switch"

# ---------------------------------------------------------------------------
# Destratification / cooling fan mappings (Shelly Pro 2PM + ceiling sensor)
# ---------------------------------------------------------------------------
# The Shelly script owns ALL fan timing and safety. Home Assistant only decides
# when the fans are wanted and in which direction; it never reproduces the dwell
# or the interlock. See docs/BEHAVIOUR.md.
CONF_CEILING_TEMP = "ceiling_temp"          # local ceiling temperature sensor
CONF_FLOOR_TEMP = "floor_temp"              # optional floor-temp override sensor
CONF_FAN_MASTER = "fan_master"              # Shelly O1 master (feeds transformer)
CONF_FAN_DIRECTION = "fan_direction"        # Shelly O2 direction coil
CONF_FAN_REVERSE = "fan_reverse"            # virtual reverse button (id 200)
CONF_FAN_O1_POWER = "fan_o1_power"          # O1 power (transformer + 3 fans)
CONF_FAN_O2_POWER = "fan_o2_power"          # O2 power (direction coil)
CONF_FAN_FAULT = "fan_fault"                # Shelly latched fault (if published)
CONF_ROINTE_POWER = "rointe_power_sensors"  # Rointe Effective Power sensors

# Above this floor temperature a fan blows air hotter than skin onto people and
# makes them warmer, not cooler; the summer fans hold off and a notification
# suggests ventilation/shade instead. 35 °C per UK public-health guidance
# (CDC advises 32 °C where occupants may be vulnerable). Not tunable: this is
# a health limit, not a preference.
FAN_COOLING_MAX_TEMP = 35.0

# Every mapping key, grouped for the config flow / options flow.
MULTI_ENTITY_KEYS = (
    CONF_HALL_CLIMATES,
    CONF_OFFICE_CLIMATES,
    CONF_SHARED_CLIMATES,
    CONF_HALL_COMFORT_NUMBERS,
    CONF_HALL_ECO_NUMBERS,
    CONF_ZONE_A_DOORS,
    CONF_ZONE_A_WINDOWS,
    CONF_ZONE_B_DOORS,
    CONF_ZONE_B_WINDOWS,
    CONF_SHARED_WINDOWS,
    CONF_ROINTE_POWER,
)

SINGLE_ENTITY_KEYS = (
    CONF_MOTION_HALL,
    CONF_MOTION_OFFICE,
    CONF_MOTION_KITCHEN,
    CONF_MOTION_GENTS,
    CONF_MOTION_FEMALE,
    CONF_INTERNAL_DOOR,
    CONF_CALENDAR_HALL,
    CONF_CALENDAR_OFFICE,
    CONF_WEATHER,
    CONF_REALFEEL,
    CONF_ALARM_MAIN,
    CONF_ALARM_OFFICE,
    CONF_WATER_SWITCH,
    CONF_CEILING_TEMP,
    CONF_FLOOR_TEMP,
    CONF_FAN_MASTER,
    CONF_FAN_DIRECTION,
    CONF_FAN_REVERSE,
    CONF_FAN_O1_POWER,
    CONF_FAN_O2_POWER,
    CONF_FAN_FAULT,
)

# Optional single-entity keys (setup can complete without them).
OPTIONAL_KEYS = (
    CONF_INTERNAL_DOOR,
    CONF_REALFEEL,
    CONF_ALARM_MAIN,
    CONF_ALARM_OFFICE,
    CONF_HALL_COMFORT_NUMBERS,
    CONF_HALL_ECO_NUMBERS,
    CONF_SHARED_CLIMATES,
    CONF_WATER_SWITCH,
    CONF_MOTION_KITCHEN,
    CONF_MOTION_GENTS,
    CONF_MOTION_FEMALE,
    # Fan / cooling mappings are all additive and guarded, so the whole feature
    # is absent-safe: an entry made before the fans step existed keeps working.
    CONF_CEILING_TEMP,
    CONF_FLOOR_TEMP,
    CONF_FAN_MASTER,
    CONF_FAN_DIRECTION,
    CONF_FAN_REVERSE,
    CONF_FAN_O1_POWER,
    CONF_FAN_O2_POWER,
    CONF_FAN_FAULT,
    CONF_ROINTE_POWER,
)

# ---------------------------------------------------------------------------
# Tunable helper entities owned by this integration
# ---------------------------------------------------------------------------
# key: (min, max, step, default, unit)
NUMBER_DEFS: dict[str, tuple[float, float, float, float, str | None]] = {
    # Maximum pre-heat lead. The actual lead is computed adaptively per zone
    # (learned warm-up rate x temperature deficit, with a cold-weather margin)
    # and clamped to this — the optimum-start cap, like Rointe's own 2 h limit.
    # Slider maximum widened to 240: commercial guidance (Heatmiser) suggests
    # cold buildings can need ~3 h of preheat, and this hall from a 7 °C
    # winter start may prove it. The default stays 120 until the winter
    # booking_start shortfalls say otherwise.
    "preheat_minutes": (0, 240, 5, 120, "min"),
    # Learned warm-up rates (minutes per °C), one per zone (plus a separate
    # hall rate for when the destratification fans are running, which
    # materially speeds warm-up). Updated automatically from every completed
    # comfort warm-up; adjustable to re-seed after building changes.
    # DELIBERATELY seeded at the slowest plausible rate: an unlearned zone
    # therefore uses (effectively) the full pre-heat cap — fail-safe warm —
    # and real warm-ups pull the rate down to the truth over a few bookings.
    "zone_a_warmup_rate": (5, 60, 0.5, 60, "min/°C"),
    "zone_a_warmup_rate_fans": (5, 60, 0.5, 60, "min/°C"),
    "zone_b_warmup_rate": (5, 60, 0.5, 60, "min/°C"),
    # Learned heat-loss constants: the PERCENTAGE of the indoor-outdoor
    # temperature gap a zone loses per hour while coasting unheated (Newton
    # cooling). Gap-normalising is what lets summer cool-offs teach winter
    # predictions — the constant is a property of the fabric, not the season.
    # Measured July 2026: hall ~10 %/h, insulated office ~4.5 %/h. Used to
    # predict how far a room will have cooled by the time a pre-heat begins.
    # Seeded lossy (cautious: predicts colder, heats earlier); set to 0 to
    # disable the prediction.
    "zone_a_heatloss_pct": (0, 50, 0.5, 25, "%/h"),
    "zone_b_heatloss_pct": (0, 50, 0.5, 25, "%/h"),
    "motion_timeout_minutes": (5, 60, 5, 15, "min"),
    "door_ice_minutes": (2, 30, 1, 10, "min"),
    "window_ice_minutes": (5, 60, 5, 10, "min"),
    "seasonal_lockout_temp": (10, 20, 0.5, 15, "°C"),
    # Hall setpoint sliders are bounded to what the Rointe number entities
    # accept (comfort 19-30 °C, eco 7.5-18.5 °C per the official manuals), so a
    # slider value can never be silently rejected by the heater.
    #
    # Defaults are sized for ACTIVE hall use, not classroom sitting: CIBSE
    # Guide A holds higher-activity spaces at ~17 °C, sports-hall practice is
    # 12-16 °C, and the 18 °C classroom floor applies to the most sedentary
    # regulars (crafts, closing circle, parents at the edge). Comfort 19.5
    # serves those; games sessions self-heat well past comfort at that level,
    # and pure-activity lettings use the eco-low keyword tier (14 °C, inside
    # the active-sports band). Eco 16 is the occupied-adjacent resting point
    # (circulation-space territory, an easy recovery to comfort).
    "hall_comfort_temp": (19, 24, 0.5, 19.5, "°C"),
    "hall_eco_temp": (10, 18.5, 0.5, 16, "°C"),
    "hall_eco_low_temp": (8, 18, 0.5, 14, "°C"),
    # The 15 L / 2 kW Speedflow needs ~30 min from cold to reach temperature,
    # so the default matches a full reheat and the range leaves headroom.
    "water_preheat_minutes": (5, 60, 5, 30, "min"),
    "water_motion_keepalive_minutes": (15, 120, 15, 60, "min"),
    # --- Destratification / cooling fan tunables ---
    # Ceiling-minus-floor difference to start (dt_on) and stop (dt_off) the
    # winter fans. The gap between them is the hysteresis band. Defaults are
    # tuned for THIS hall (under-radiatored, every recovered degree matters):
    # start earlier and run closer to fully mixed than the generic 3/1
    # practice, still within the documented band (Reznor 2-3 °C rule,
    # CaptiveAire 0.5-1 °C hysteresis).
    "fan_dt_on": (0.5, 10, 0.5, 2, "°C"),
    "fan_dt_off": (0, 5, 0.5, 0.5, "°C"),
    # Anti-short-cycle timers.
    "fan_min_run_minutes": (0, 60, 1, 10, "min"),
    "fan_min_off_minutes": (0, 60, 1, 10, "min"),
    # Ceiling/floor reading older than this (no report) counts as lost. The
    # default suits a battery Shelly H&T, which sleeps aggressively and can go
    # well over an hour between reports when the temperature is steady.
    "fan_sensor_stale_minutes": (5, 240, 5, 120, "min"),
    # Summer: the head-height comfort estimate (0.75 x floor + 0.25 x ceiling —
    # the air an occupant actually feels) above this is "warm enough" to want a
    # breeze. Judged there, not at the low floor sensor, which under-reads the
    # occupied room on a still hot day under a hot ceiling. A degree under the
    # sedentary norm because hall users are active — moving bodies want the
    # airflow earlier than seated ones.
    "cooling_temp_high": (18, 30, 0.5, 23, "°C"),
    # Hot-breeze guard: hold the summer fans (and suggest opening the doors)
    # once the MIXED air they would fold down to head height — estimated as
    # 0.75 x floor + 0.25 x ceiling — reaches this. Between here and the hard
    # 35 °C floor cutoff a breeze gives rapidly diminishing benefit (CDC
    # advises not relying on fans from 32 °C for vulnerable occupants — and
    # this is a children's building). Release 1 °C below to avoid flapping.
    "cooling_mix_max_temp": (26, 34, 0.5, 29, "°C"),
    # A Rointe Effective Power reading above this means that heater is calling.
    "heat_demand_watts": (0, 200, 5, 20, "W"),
    # Winter fans recirculate ceiling heat while the floor is below this cap, even
    # after the heaters cut out (harvesting residual / leaked heat). Above it the
    # occupied zone is warm enough that there is nothing to gain. 24 °C mirrors the
    # 75 °F top of the documented HVLS winter-mode band.
    "fan_recirc_max_floor_temp": (18, 28, 0.5, 24, "°C"),
    # --- Drive-to-target (outer setpoint trim loop) ---
    # Max °C the loop may push a heater's setpoint ABOVE its target to force the
    # Rointe (which settles a fraction under) to actually reach target. The room
    # does NOT reach this — the loop backs off at target — so it only bites if a
    # probe reads falsely low: the safety envelope. 4.5 puts the hall cap at its
    # existing comfort ceiling (19.5 + 4.5 = 24).
    "drive_max_offset": (0, 6, 0.5, 4.5, "°C"),
    # Comfort setpoints the integration drives the OFFICE and SHARED heaters to
    # (the hall uses hall_comfort_temp). Owning these is what lets the loop drive
    # those zones — and finally gives office/shared their own setpoint sliders,
    # which the device otherwise kept to itself.
    "office_comfort_temp": (19, 24, 0.5, 19.5, "°C"),
    "shared_comfort_temp": (19, 24, 0.5, 19.5, "°C"),
    # How much ABOVE the comfort target a Boost drives the room. A boost is an
    # occupant saying "still too cold" — so it must aim higher than the standing
    # comfort setpoint, or pressing it while already at comfort would do nothing
    # (the drive is already targeting comfort). The drive chases comfort + this
    # offset for the boost duration, then reverts. Clamped to the 30 °C Rointe
    # max downstream. 2 °C is a measured bump; raise it for a more aggressive
    # boost (Q19: felt-cold at a satisfied 19.5 is real in this cold-radiant hall).
    "boost_offset": (1, 5, 0.5, 2.0, "°C"),
    # Ceiling on the booking "hold" margin — how far above comfort the drive may
    # hold the hall during a running booking to stop it dipping below comfort as
    # the evening cools. The margin itself is sized automatically from the learned
    # cool-off and warm-up rates (preheat.hold_margin); this is the cap and the
    # off switch (0 disables the hold entirely). Hall only.
    "booking_hold_cap": (0, 3, 0.5, 1.5, "°C"),
}

NUMBER_ICONS: dict[str, str] = {
    "preheat_minutes": "mdi:radiator",
    "zone_a_warmup_rate": "mdi:chart-line",
    "zone_a_warmup_rate_fans": "mdi:fan-chevron-up",
    "zone_b_warmup_rate": "mdi:chart-line",
    "zone_a_heatloss_pct": "mdi:snowflake-thermometer",
    "zone_b_heatloss_pct": "mdi:snowflake-thermometer",
    "motion_timeout_minutes": "mdi:timer-outline",
    "door_ice_minutes": "mdi:door-open",
    "window_ice_minutes": "mdi:window-open",
    "seasonal_lockout_temp": "mdi:thermometer-chevron-up",
    "hall_comfort_temp": "mdi:thermometer-high",
    "hall_eco_temp": "mdi:thermometer-low",
    "hall_eco_low_temp": "mdi:thermometer-minus",
    "water_preheat_minutes": "mdi:water-boiler",
    "water_motion_keepalive_minutes": "mdi:timer-outline",
    "fan_dt_on": "mdi:fan-plus",
    "fan_dt_off": "mdi:fan-minus",
    "fan_min_run_minutes": "mdi:timer-play",
    "fan_min_off_minutes": "mdi:timer-off",
    "fan_sensor_stale_minutes": "mdi:timer-alert",
    "cooling_temp_high": "mdi:thermometer-high",
    "cooling_mix_max_temp": "mdi:weather-windy-variant",
    "heat_demand_watts": "mdi:flash",
    "fan_recirc_max_floor_temp": "mdi:thermometer-chevron-up",
    "drive_max_offset": "mdi:thermometer-chevron-up",
    "office_comfort_temp": "mdi:thermometer-high",
    "shared_comfort_temp": "mdi:thermometer-high",
    "boost_offset": "mdi:thermometer-plus",
    "booking_hold_cap": "mdi:thermometer-chevron-up",
}

BOOST_OPTIONS = ["30 min", "60 min", "90 min"]
BOOST_DEFAULT = "60 min"

# Cooling direction is fully automatic — no toggle, no season. The fans read the
# room and decide (`_fan_cooling_regime` / `fan_decision`): active heating always
# destratifies (reverse), a genuinely warm occupied hall gets a cooling breeze
# (forward), a cool one destratifies. COOLING_DIRECTION_HYST is the hysteresis
# band on the warm/cool boundary: once cooling has started the room must drop
# this far below `cooling_temp_high` before the direction flips back, so a hall
# hovering at the threshold cannot flap the heavy fans forward<->reverse (each
# reversal is a ~5-min coast-down; reversals must stay rare).
COOLING_DIRECTION_HYST = 1.0  # °C

# User-facing switches: key -> default state (True = on)
SWITCH_DEFS: dict[str, bool] = {
    "zone_a_automation_enabled": True,
    "zone_b_automation_enabled": True,
    "zone_a_occupied_override": False,
    "zone_b_occupied_override": False,
    "water_manual_override": False,
    # Master enable for the destratification fans (winter). Default on.
    "fans_enabled": True,
    # The old summer_mode / summer_follows_season / fans_follow_state switches are
    # gone: fan direction is fully automatic from live room state, no toggle.
    # When the ceiling/floor sensor is lost, assume stratification is present and
    # keep running the winter fans (still gated by heat demand) rather than
    # failing to off. Default ON per site preference; turn off to fail-safe
    # to fans-off instead. The Shelly still owns all motor safety either way.
    "fans_run_on_sensor_loss": True,
    # Actively drive each heater's setpoint UP until its OWN probe reaches
    # target, instead of trusting the Rointe (which settles a fraction under and
    # reports a modelled "full" power that the radiators do not match). A
    # per-heater staircase loop with a heat-loss feedforward and a full safety
    # net (freshness gate, cross-probe sanity, cap, cap-pinned alert, last-will
    # reset). Default ON; OFF restores plain "push the target and trust it".
    "drive_to_target": True,
    # "Will it get there on its own?" During a hall pre-heat window or a running
    # occupied booking, if the room is *measurably* warming on free gain (sun,
    # occupancy, warm fabric) fast enough to reach the comfort band with a margin,
    # hold at eco instead of firing the radiators. Comfort-lean (only on an
    # OBSERVED idle-room climb, never a guess), re-evaluated every tick. Default
    # OFF — the one feature that can WITHHOLD heat (a wrong call = a cold
    # arrival), so it stays opt-in until validated against a real winter export.
    "coast_when_free": False,
    # NOTE: cold_booking_heats, drive_self_check, winter_fans_need_occupancy and
    # summer_setback_mode were switches; they are gone. The first three became
    # permanent behaviour (always-on with no realistic reason to disable);
    # summer_setback_mode was subsumed when heating was decoupled from the season
    # entirely — an occupied cool hall now heats toward comfort on its own, no
    # toggle. The cooling-direction switches (summer_mode / summer_follows_season
    # / fans_follow_state) are likewise gone: fan direction is fully automatic
    # from live room state (`_fan_cooling_regime`).
}

SWITCH_ICONS: dict[str, str] = {
    "zone_a_automation_enabled": "mdi:calendar-check",
    "zone_b_automation_enabled": "mdi:calendar-check",
    "zone_a_occupied_override": "mdi:account-check",
    "zone_b_occupied_override": "mdi:account-check",
    "water_manual_override": "mdi:water-boiler-alert",
    "fans_enabled": "mdi:ceiling-fan",
    "fans_run_on_sensor_loss": "mdi:fan-alert",
    "drive_to_target": "mdi:thermometer-auto",
    "coast_when_free": "mdi:weather-sunny-alert",
}

# Selects: key -> (options, default).
SELECT_DEFS: dict[str, tuple[list[str], str]] = {
    "boost_duration": (BOOST_OPTIONS, BOOST_DEFAULT),
}

SELECT_ICONS: dict[str, str] = {
    "boost_duration": "mdi:fire",
}

SELECT_NAMES: dict[str, str] = {
    "boost_duration": "Boost duration",
}

DEFAULT_ECO_KEYWORDS = "sal-vation,test"

# Zones handled by the reconciler.
ZONE_A = "zone_a"
ZONE_B = "zone_b"

# Persistent notification ids (kept stable so we can dismiss them).
NOTIFY_ZONE_OPENING = {
    ZONE_A: "scout_zone_a_opening_ice",
    ZONE_B: "scout_zone_b_opening_ice",
}
NOTIFY_ZONE_HOLD = {
    ZONE_A: "scout_zone_a_manual_hold",
    ZONE_B: "scout_zone_b_manual_hold",
}
NOTIFY_SHARED_OPENING = "scout_shared_opening_ice"
NOTIFY_INTERNAL_DOOR = "scout_internal_door_exterior_open"
NOTIFY_SEASONAL = "scout_seasonal_lockout"
NOTIFY_FAN_FAULT = "scout_fan_fault"
NOTIFY_FAN_DIAL = "scout_fan_dial_high"
NOTIFY_FAN_SENSOR_LOST = "scout_fan_sensor_lost"
NOTIFY_FAN_TOO_HOT = "scout_fan_too_hot"
NOTIFY_FAN_BREEZE = "scout_fan_breeze_hot"
NOTIFY_CONDENSATION = "scout_condensation"
NOTIFY_DRIVE_CAPPED = "scout_drive_capped"
NOTIFY_DRIVE_REJECTED = "scout_drive_setpoint_rejected"
NOTIFY_DRIVE_NO_RESPONSE = "scout_drive_no_response"
NOTIFY_DASHBOARDS = "scout_dashboards"
NOTIFY_FIRE = "scout_fire_hold"

# Fire fallback: the scouthut-alarmnotification (Texecom Alerts) integration
# fires this bus event for every classified panel event; a fire carries an
# `event_type` in FIRE_EVENT_TYPES. The 230 V fan supply is hardware-cut on a
# fire output (the real safety), but HA does not otherwise know a fire happened
# — so it listens for this as a software fallback and holds EVERYTHING off
# (heating -> ice, water off, fans off) until a person clears it. No-op if the
# alarm integration is not installed (the event simply never fires).
FIRE_EVENT = "texecom_alerts_event"
FIRE_EVENT_TYPES = frozenset({"Fire", "KeypadFire"})

NOTIFY_OPENING_INFERRED = {
    ZONE_A: "scout_zone_a_opening_inferred",
    ZONE_B: "scout_zone_b_opening_inferred",
}
