"""Alarm-state awareness: an *away* arm suppresses heat, *night* does not.

The heating reads a real ``alarm_control_panel`` so ``armed_night`` (people
sleeping inside — e.g. a sleepover) keeps the heat on, while ``armed_away``
(empty building) drops the zone to ice. A legacy binary ``on`` still means
armed-away, so installs feeding the alarm through an input_boolean/helper keep
their pre-1.12 behaviour.
"""

from scout_testkit import (
    E,
    PRESET_ECO,
    PRESET_ICE,
    ZA,
    make_controller,
    motion,
    on,
)


def _set(hass, entity, state):
    hass.states.set(entity, state)


# --- Per-zone ---------------------------------------------------------------
def test_armed_away_suppresses_an_occupied_zone():
    ctrl, hass = make_controller()
    _set(hass, E["alarm_main"], "armed_away")
    motion(ctrl, "hall")
    assert ctrl._desired_zone(ZA) == PRESET_ICE


def test_armed_night_keeps_heating_an_occupied_zone():
    ctrl, hass = make_controller()
    _set(hass, E["alarm_main"], "armed_night")  # sleepover: people inside
    motion(ctrl, "hall")
    assert ctrl._desired_zone(ZA) == PRESET_ECO  # not iced by the alarm


def test_armed_vacation_suppresses_like_away():
    ctrl, hass = make_controller()
    _set(hass, E["alarm_main"], "armed_vacation")
    motion(ctrl, "hall")
    assert ctrl._desired_zone(ZA) == PRESET_ICE


def test_disarmed_does_not_suppress():
    ctrl, hass = make_controller()
    _set(hass, E["alarm_main"], "disarmed")
    motion(ctrl, "hall")
    assert ctrl._desired_zone(ZA) == PRESET_ECO


def test_triggered_leaves_heating_unchanged():
    ctrl, hass = make_controller()
    _set(hass, E["alarm_main"], "triggered")  # alarm going off, not "empty"
    motion(ctrl, "hall")
    assert ctrl._desired_zone(ZA) == PRESET_ECO


def test_legacy_binary_on_still_means_armed_away():
    ctrl, hass = make_controller()
    on(hass, E["alarm_main"])  # input_boolean/helper install: "on" = armed
    motion(ctrl, "hall")
    assert ctrl._desired_zone(ZA) == PRESET_ICE


# --- Shared zone (needs BOTH panels away) -----------------------------------
def test_both_away_ices_shared():
    ctrl, hass = make_controller()
    _set(hass, E["alarm_main"], "armed_away")
    _set(hass, E["alarm_office"], "armed_away")
    motion(ctrl, "kitchen")
    assert ctrl._desired_shared() == PRESET_ICE


def test_both_night_keeps_shared_heating():
    ctrl, hass = make_controller()
    _set(hass, E["alarm_main"], "armed_night")
    _set(hass, E["alarm_office"], "armed_night")
    motion(ctrl, "kitchen")
    assert ctrl._desired_shared() == PRESET_ECO


def test_one_away_one_night_does_not_ice_shared():
    ctrl, hass = make_controller()
    _set(hass, E["alarm_main"], "armed_away")
    _set(hass, E["alarm_office"], "armed_night")
    motion(ctrl, "kitchen")
    assert ctrl._desired_shared() == PRESET_ECO


# --- Water heater (needs BOTH panels away to suppress motion) ---------------
def test_both_away_suppresses_water_on_motion():
    ctrl, hass = make_controller()
    _set(hass, E["alarm_main"], "armed_away")
    _set(hass, E["alarm_office"], "armed_away")
    motion(ctrl, "kitchen")
    assert ctrl._desired_water() is False


def test_both_night_lets_water_run_on_motion():
    ctrl, hass = make_controller()
    _set(hass, E["alarm_main"], "armed_night")
    _set(hass, E["alarm_office"], "armed_night")
    motion(ctrl, "kitchen")
    assert ctrl._desired_water() is True
