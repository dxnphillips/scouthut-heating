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
