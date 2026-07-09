"""Reset-to-defaults button and clamped number restore."""

from scout_testkit import make_controller, run
from custom_components.scout_hut_heating.const import NUMBER_DEFS
from custom_components.scout_hut_heating.number import ScoutNumber


def test_reset_tunables_restores_every_default():
    ctrl, _ = make_controller()
    ctrl._numbers["preheat_minutes"].native_value = 45
    ctrl._numbers["fan_dt_on"].native_value = 8
    ctrl._switches["summer_mode"].is_on = True
    ctrl._switches["zone_a_automation_enabled"].is_on = False
    ctrl._selects["boost_duration"].current_option = "90 min"
    ctrl._texts["eco_keywords"].native_value = "something,else"
    run(ctrl.async_reset_tunables())
    assert ctrl.number("preheat_minutes") == NUMBER_DEFS["preheat_minutes"][3]
    assert ctrl.number("fan_dt_on") == NUMBER_DEFS["fan_dt_on"][3]
    assert ctrl.switch_on("summer_mode") is False
    assert ctrl.switch_on("zone_a_automation_enabled") is True
    assert ctrl.boost_minutes() == 60
    assert ctrl.eco_keywords() == ["sal-vation", "test"]


def test_reset_does_not_clear_a_latched_fan_fault():
    # Resetting the tunables must not act as the deliberate fault re-arm.
    ctrl, _ = make_controller()
    ctrl.fan_fault_latched = True
    ctrl._switches["fans_enabled"].is_on = False
    run(ctrl.async_reset_tunables())
    assert ctrl.switch_on("fans_enabled") is True  # default restored
    assert ctrl.fan_fault_latched is True  # latch untouched


def test_restored_number_out_of_range_is_clamped(monkeypatch):
    # An upgrade tightened the hall comfort minimum to 19 (the Rointe floor);
    # a previously stored 16 must clamp on restore rather than survive.
    ctrl, _ = make_controller()
    ent = ScoutNumber(ctrl, "hall_comfort_temp")

    class _Data:
        native_value = 16.0

    async def _last_data():
        return _Data()

    monkeypatch.setattr(ent, "async_get_last_number_data", _last_data)
    run(ent.async_added_to_hass())
    assert ent._attr_native_value == 19.0


def test_restored_number_in_range_is_kept(monkeypatch):
    ctrl, _ = make_controller()
    ent = ScoutNumber(ctrl, "hall_comfort_temp")

    class _Data:
        native_value = 21.5

    async def _last_data():
        return _Data()

    monkeypatch.setattr(ent, "async_get_last_number_data", _last_data)
    run(ent.async_added_to_hass())
    assert ent._attr_native_value == 21.5
