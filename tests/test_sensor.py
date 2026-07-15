"""Diagnostic sensor value getters."""

from custom_components.scout_hut_heating.sensor import SENSORS
from scout_testkit import make_controller


def _value(ctrl, key):
    return SENSORS[key][2](ctrl)


def test_fan_mix_sensor_reports_rounded_head_height_mix():
    ctrl, _ = make_controller()
    ctrl.fan_mix = 23.1499
    assert _value(ctrl, "fan_mix") == "23.1"


def test_fan_mix_sensor_is_none_when_unavailable():
    ctrl, _ = make_controller()
    ctrl.fan_mix = None
    assert _value(ctrl, "fan_mix") is None
