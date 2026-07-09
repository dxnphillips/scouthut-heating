"""Water heater desired-state logic."""

from scout_testkit import advance, make_controller, motion, on, run, E
from custom_components.scout_hut_heating.const import CONF_WATER_SWITCH


def test_no_switch_returns_none():
    ctrl, _ = make_controller({CONF_WATER_SWITCH: None})
    assert ctrl._desired_water() is None


def test_empty_is_off():
    ctrl, _ = make_controller()
    assert ctrl._desired_water() is False


def test_manual_override_is_on():
    ctrl, _ = make_controller()
    ctrl._switches["water_manual_override"].is_on = True
    assert ctrl._desired_water() is True


def test_calendar_window_is_on():
    ctrl, _ = make_controller()
    ctrl.water_window = True
    assert ctrl._desired_water() is True


def test_kitchen_motion_is_on():
    ctrl, _ = make_controller()
    motion(ctrl, "kitchen")
    assert ctrl._desired_water() is True


def test_gents_motion_is_on():
    ctrl, _ = make_controller()
    motion(ctrl, "gents")
    assert ctrl._desired_water() is True


def test_female_motion_is_on():
    ctrl, _ = make_controller()
    motion(ctrl, "female")
    assert ctrl._desired_water() is True


def test_hall_motion_does_not_keep_water_on():
    # The water heater only follows kitchen / toilet motion, not the hall/office.
    ctrl, _ = make_controller()
    motion(ctrl, "hall")
    motion(ctrl, "office")
    assert ctrl._desired_water() is False


def test_both_alarms_suppress_motion():
    ctrl, hass = make_controller()
    on(hass, E["alarm_main"])
    on(hass, E["alarm_office"])
    motion(ctrl, "kitchen")
    assert ctrl._desired_water() is False


def test_both_alarms_but_override_is_on():
    ctrl, hass = make_controller()
    on(hass, E["alarm_main"])
    on(hass, E["alarm_office"])
    ctrl._switches["water_manual_override"].is_on = True
    assert ctrl._desired_water() is True


def test_both_alarms_but_calendar_is_on():
    ctrl, hass = make_controller()
    on(hass, E["alarm_main"])
    on(hass, E["alarm_office"])
    ctrl.water_window = True
    assert ctrl._desired_water() is True


def test_single_alarm_does_not_suppress_motion():
    ctrl, hass = make_controller()
    on(hass, E["alarm_main"])
    motion(ctrl, "kitchen")
    assert ctrl._desired_water() is True


# --- Frost protection (the Speedflow's own frost stat needs power) -----------

def _shared_temp(hass, temp):
    hass.states.set(E["shared"][0], "heat", {"current_temperature": temp})


def test_frost_powers_water_even_when_alarmed():
    ctrl, hass = make_controller()
    on(hass, E["alarm_main"])
    on(hass, E["alarm_office"])
    _shared_temp(hass, 2.0)
    assert ctrl._desired_water() is True


def test_frost_holds_through_hysteresis_band_and_releases():
    ctrl, hass = make_controller()
    _shared_temp(hass, 2.0)
    assert ctrl._desired_water() is True
    _shared_temp(hass, 4.0)  # above trip, below release: stays on
    assert ctrl._desired_water() is True
    _shared_temp(hass, 6.0)  # recovered
    assert ctrl._desired_water() is False


def test_coldest_shared_room_wins():
    ctrl, hass = make_controller()
    hass.states.set(E["shared"][0], "heat", {"current_temperature": 8.0})
    hass.states.set(E["shared"][1], "heat", {"current_temperature": 2.5})
    assert ctrl._desired_water() is True


def test_normal_room_temperature_does_not_trip_frost():
    ctrl, hass = make_controller()
    _shared_temp(hass, 8.0)
    assert ctrl._desired_water() is False


# --- Weekly hygiene heat-up ---------------------------------------------------

def test_hygiene_cycle_after_a_quiet_week():
    ctrl, _ = make_controller()
    run(ctrl.async_reconcile())  # starts the clock; nothing on
    assert ctrl.water_on is False
    advance(ctrl, 8 * 24 * 60)  # eight quiet days
    run(ctrl.async_reconcile())
    assert ctrl.water_on is True
    assert ctrl.water_hygiene_until is not None


def test_hygiene_cycle_ends_after_its_window():
    ctrl, _ = make_controller()
    run(ctrl.async_reconcile())
    advance(ctrl, 8 * 24 * 60)
    run(ctrl.async_reconcile())  # hygiene on
    advance(ctrl, 60)  # past the 45 min window
    run(ctrl.async_reconcile())
    assert ctrl.water_on is False


def test_hygiene_runs_even_when_alarmed():
    ctrl, hass = make_controller()
    run(ctrl.async_reconcile())
    on(hass, E["alarm_main"])
    on(hass, E["alarm_office"])
    advance(ctrl, 8 * 24 * 60)
    assert ctrl._desired_water() is True


def test_regular_use_defers_the_hygiene_cycle():
    ctrl, _ = make_controller()
    run(ctrl.async_reconcile())
    advance(ctrl, 5 * 24 * 60)  # five days quiet
    motion(ctrl, "kitchen")  # normal use powers the tank
    run(ctrl.async_reconcile())
    assert ctrl.water_on is True and ctrl.water_hygiene_until is None
    advance(ctrl, 5 * 24 * 60)  # five more days: only five since last hot
    run(ctrl.async_reconcile())
    assert ctrl.water_on is False
