"""Destrat recirculation chases the hall's desired setpoint, not a fixed cap.

The fix: the winter recirc path harvests stored ceiling heat only toward the
temperature the hall is actually being driven to (the applied preset's
setpoint). So an eco-low booking whose room already sits above its low target
(applied = ice) does NOT get unwanted ceiling heat destratified onto occupants
who asked for it cool — while a genuine heating goal still runs the fans.
"""

from custom_components.scout_hut_heating.const import (
    CONF_CEILING_TEMP,
    CONF_FAN_MASTER,
    CONF_ROINTE_POWER,
)
from scout_testkit import E, ZA, make_controller, motion, off

MASTER = "switch.fan_master"
POWER = "sensor.hall_power"


def _mk():
    ctrl, hass = make_controller(
        config_overrides={
            CONF_FAN_MASTER: MASTER,
            CONF_CEILING_TEMP: "sensor.ceiling",
            CONF_ROINTE_POWER: [POWER],
        }
    )
    off(hass, MASTER)
    hass.states.set(POWER, "0")  # heaters idle -> no demand (real, not the fallback)
    ctrl.seasonal_lockout = False
    return ctrl, hass


def _hall(ctrl, floor, ceiling):
    for eid in E["hall"]:
        ctrl.hass.states.set(eid, "heat", {"current_temperature": floor})
    ctrl.hass.states.set("sensor.ceiling", str(ceiling))


# --- _hall_desired_setpoint -------------------------------------------------
def test_desired_setpoint_tracks_the_applied_preset():
    ctrl, _ = _mk()
    ctrl.applied[ZA] = "ice"
    assert ctrl._hall_desired_setpoint() == 7.0  # anti-frost: no heating goal
    ctrl.applied[ZA] = "comfort"
    assert ctrl._hall_desired_setpoint() == ctrl.number("hall_comfort_temp")  # 19.5
    ctrl.applied[ZA] = "eco"
    ctrl.cal_title[ZA] = ""  # ordinary eco
    assert ctrl._hall_desired_setpoint() == ctrl.number("hall_eco_temp")  # 16
    ctrl.cal_title[ZA] = "test"  # ECO-keyword -> eco-low
    assert ctrl._hall_desired_setpoint() == ctrl.number("hall_eco_low_temp")  # 14


# --- The fix: no destrat of unwanted heat ----------------------------------
def test_eco_low_booking_above_target_does_not_destratify():
    """The export case: room 16 (above the 14 they asked for), occupied,
    stratified, but applied ice -> the fans must NOT run."""
    ctrl, _ = _mk()
    ctrl.applied[ZA] = "ice"  # room already above the eco-low target -> frozen
    _hall(ctrl, floor=16.0, ceiling=19.0)  # dt 3 > dt_on, floor < the old 24 cap
    motion(ctrl, "hall")  # occupied
    assert ctrl._fan_target() == (False, None, "off")


def test_comfort_goal_below_target_still_destratifies():
    """A genuine heating goal (room below comfort) still runs the destrat fans."""
    ctrl, _ = _mk()
    ctrl.applied[ZA] = "comfort"  # goal 19.5
    _hall(ctrl, floor=17.0, ceiling=20.0)  # floor below comfort, dt 3
    motion(ctrl, "hall")
    on, direction, mode = ctrl._fan_target()
    assert on is True and direction == "reverse" and mode == "winter"


def test_eco_goal_below_target_destratifies():
    ctrl, _ = _mk()
    ctrl.applied[ZA] = "eco"  # ordinary eco goal 16
    ctrl.cal_title[ZA] = ""
    _hall(ctrl, floor=14.0, ceiling=17.0)  # floor below 16, dt 3
    motion(ctrl, "hall")
    assert ctrl._fan_target()[0] is True


def test_eco_goal_already_met_does_not_destratify():
    """Room at 16.5 with an ordinary eco (16) goal — already met -> no destrat."""
    ctrl, _ = _mk()
    ctrl.applied[ZA] = "eco"
    ctrl.cal_title[ZA] = ""
    _hall(ctrl, floor=16.5, ceiling=19.5)  # above the 16 goal, still stratified
    motion(ctrl, "hall")
    assert ctrl._fan_target() == (False, None, "off")
