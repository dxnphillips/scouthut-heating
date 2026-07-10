"""The hall number-entity overrides belong under Configure, not first-run setup."""

from custom_components.scout_hut_heating import config_flow as cf
from custom_components.scout_hut_heating.const import (
    CONF_HALL_CLIMATES,
    CONF_HALL_COMFORT_NUMBERS,
    CONF_HALL_ECO_NUMBERS,
    CONF_OFFICE_CLIMATES,
)


def _keys(schema):
    keys = set()
    for marker in schema.schema:
        keys.add(getattr(marker, "key", None) or getattr(marker, "schema", None) or str(marker))
    return keys


def test_first_run_setup_omits_number_overrides():
    keys = _keys(cf._zones_schema({}, overrides=False))
    assert CONF_HALL_CLIMATES in keys
    assert CONF_OFFICE_CLIMATES in keys
    assert CONF_HALL_COMFORT_NUMBERS not in keys
    assert CONF_HALL_ECO_NUMBERS not in keys


def test_options_flow_keeps_number_overrides():
    keys = _keys(cf._zones_schema({}, overrides=True))
    assert CONF_HALL_COMFORT_NUMBERS in keys
    assert CONF_HALL_ECO_NUMBERS in keys


def test_apply_step_removes_cleared_optionals():
    # An entity field the user clears in the UI is omitted from user_input
    # (HA selector behaviour): it must be REMOVED, not silently kept.
    from custom_components.scout_hut_heating.const import (
        CONF_MOTION_HALL,
        CONF_MOTION_KITCHEN,
        CONF_MOTION_OFFICE,
    )

    data = {
        CONF_MOTION_HALL: "binary_sensor.hall",
        CONF_MOTION_OFFICE: "binary_sensor.office",
        CONF_MOTION_KITCHEN: "binary_sensor.kitchen",
    }
    cf._apply_step(
        data,
        {  # hall re-submitted, office cleared (omitted), kitchen emptied
            CONF_MOTION_HALL: "binary_sensor.hall_new",
            CONF_MOTION_KITCHEN: "",
        },
        cf.MOTION_KEYS,
    )
    assert data == {CONF_MOTION_HALL: "binary_sensor.hall_new"}
