"""Auto-detection of hall comfort/eco number entities."""

from scout_testkit import make_controller, set_registry
from custom_components.scout_hut_heating.const import (
    CONF_HALL_CLIMATES,
    CONF_HALL_COMFORT_NUMBERS,
    CONF_HALL_ECO_NUMBERS,
)


def _wire_registry():
    set_registry(
        entries_by_device={
            "dev_back": [
                "climate.hall_back",
                "number.hall_back_comfort_temperature",
                "number.hall_back_eco_temperature",
                "number.hall_back_power_consumption",
            ],
            "dev_front": [
                "number.hall_front_comfort_temperature",
                "number.hall_front_eco_temperature",
            ],
        },
        entity_devices={
            "climate.hall_back": "dev_back",
            "climate.hall_front": "dev_front",
        },
    )


def test_auto_discovers_comfort_and_eco():
    _wire_registry()
    ctrl, _ = make_controller(
        {
            CONF_HALL_CLIMATES: ["climate.hall_back", "climate.hall_front"],
            CONF_HALL_COMFORT_NUMBERS: [],
            CONF_HALL_ECO_NUMBERS: [],
        }
    )
    comfort, eco = ctrl._hall_number_entities()
    assert comfort == [
        "number.hall_back_comfort_temperature",
        "number.hall_front_comfort_temperature",
    ]
    assert eco == [
        "number.hall_back_eco_temperature",
        "number.hall_front_eco_temperature",
    ]


def test_unrelated_numbers_excluded():
    _wire_registry()
    ctrl, _ = make_controller(
        {CONF_HALL_CLIMATES: ["climate.hall_back"], CONF_HALL_COMFORT_NUMBERS: [], CONF_HALL_ECO_NUMBERS: []}
    )
    comfort, eco = ctrl._hall_number_entities()
    assert "number.hall_back_power_consumption" not in comfort + eco


def test_explicit_mapping_overrides():
    _wire_registry()
    ctrl, _ = make_controller(
        {
            CONF_HALL_COMFORT_NUMBERS: ["number.custom_comfort"],
            CONF_HALL_ECO_NUMBERS: ["number.custom_eco"],
        }
    )
    comfort, eco = ctrl._hall_number_entities()
    assert comfort == ["number.custom_comfort"]
    assert eco == ["number.custom_eco"]


def test_partial_override_fills_other_side():
    _wire_registry()
    ctrl, _ = make_controller(
        {
            CONF_HALL_CLIMATES: ["climate.hall_back", "climate.hall_front"],
            CONF_HALL_COMFORT_NUMBERS: ["number.custom_comfort"],
            CONF_HALL_ECO_NUMBERS: [],
        }
    )
    comfort, eco = ctrl._hall_number_entities()
    assert comfort == ["number.custom_comfort"]
    assert eco == [
        "number.hall_back_eco_temperature",
        "number.hall_front_eco_temperature",
    ]


def test_power_discovery_prefers_effective_power_over_nominal():
    # Rointe devices expose both a constant nominal "power" (the rating,
    # always fresh and above the demand threshold) and the live "effective
    # power". Discovery must pick only the effective one, or heat demand
    # reads permanently on.
    ctrl, hass = make_controller()
    set_registry(
        entries_by_device={
            "dev_hall_back": [
                "sensor.hall_back_power",           # nominal rating (constant)
                "sensor.hall_back_effective_power",  # live element draw
                "sensor.hall_back_energy",
            ],
        },
        entity_devices={"climate.hall_back": "dev_hall_back"},
    )
    assert ctrl._power_sensors() == ["sensor.hall_back_effective_power"]

    hass.states.set("sensor.hall_back_power", "1300")  # rating, fresh
    hass.states.set("sensor.hall_back_effective_power", "0")  # element idle
    assert ctrl._heat_demand() is False


def test_power_discovery_falls_back_without_an_effective_sibling():
    ctrl, _ = make_controller()
    set_registry(
        entries_by_device={
            "dev_hall_back": ["sensor.hall_back_power"],
        },
        entity_devices={"climate.hall_back": "dev_hall_back"},
    )
    assert ctrl._power_sensors() == ["sensor.hall_back_power"]


def _wire_hall_heater_sensors():
    # Both hall heaters, each with the Rointe sibling sensors seen in the field.
    set_registry(
        entries_by_device={
            "dev_hall_back": [
                "sensor.hall_back_heating_status",
                "sensor.hall_back_energy",
                "sensor.hall_back_surface_temperature",
                "sensor.hall_back_effective_power",
            ],
            "dev_hall_front": [
                "sensor.hall_front_heating_status",
                "sensor.hall_front_energy",
            ],
        },
        entity_devices={
            "climate.hall_back": "dev_hall_back",
            "climate.hall_front": "dev_hall_front",
        },
    )


def test_heater_sensor_discovery_finds_the_rointe_siblings():
    ctrl, _ = make_controller()
    _wire_hall_heater_sensors()
    assert (
        ctrl._heater_sensor("climate.hall_back", "heating_status")
        == "sensor.hall_back_heating_status"
    )
    assert ctrl._heater_sensor("climate.hall_back", "energy") == "sensor.hall_back_energy"
    assert (
        ctrl._heater_sensor("climate.hall_back", "surface")
        == "sensor.hall_back_surface_temperature"
    )
    assert (
        ctrl._heater_sensor("climate.hall_back", "effective")
        == "sensor.hall_back_effective_power"
    )
    # The front heater has no surface sensor mapped -> that key is simply absent.
    assert ctrl._heater_sensor("climate.hall_front", "surface") is None


def test_trace_records_maintaining_count_and_hall_kwh():
    ctrl, hass = make_controller()
    _wire_hall_heater_sensors()
    # One hall heater throttled (maintaining), the other at full heating.
    hass.states.set("sensor.hall_back_heating_status", "maintaining")
    hass.states.set("sensor.hall_front_heating_status", "heating")
    hass.states.set("sensor.hall_back_energy", "4.0")
    hass.states.set("sensor.hall_front_energy", "6.0")

    ctrl._sample_trace()
    (point,) = ctrl.trace.to_list()
    assert point["hall_maint"] == 1  # only the throttled one
    assert point["hall_kwh"] == 10.0  # summed accumulators


def test_heater_detail_includes_rointe_sensors_in_snapshot():
    ctrl, hass = make_controller()
    _wire_hall_heater_sensors()
    hass.states.set("sensor.hall_back_heating_status", "heating")
    hass.states.set("sensor.hall_back_energy", "4.167")
    hass.states.set("sensor.hall_back_surface_temperature", "19.0")
    hass.states.set("sensor.hall_back_effective_power", "0")

    detail = ctrl._heater_detail("climate.hall_back")
    assert detail["heating_status"] == "heating"
    assert detail["energy"] == 4.167
    assert detail["surface"] == 19.0
    assert detail["effective"] == 0.0
