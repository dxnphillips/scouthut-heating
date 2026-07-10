"""The generated dashboard config and its graceful failure path."""

from custom_components.scout_hut_heating.dashboards import build_config
from scout_testkit import make_controller, run


def _titles(view):
    return [c.get("title") for c in view["cards"]]


def test_build_config_resolves_real_entity_ids():
    emap = {
        "zone_a_status": "sensor.x_hall_preset",
        "hall_comfort_temp": "number.x_hall_comfort",
        "fans_enabled": "switch.x_fans",
        "fan_delta_t": "sensor.x_dt",
        "hall_temp_spread": "sensor.x_spread",
    }
    mapped = {"hall_climates": ["climate.a", "climate.b"], "fan_master": "switch.m"}
    config = build_config(emap, mapped)
    heating, fans = config["views"]
    assert {"entity": "sensor.x_hall_preset", "name": "Hall preset"} in heating["cards"][0]["entities"]
    # Radiators card lists the mapped climates verbatim.
    radiators = next(c for c in heating["cards"] if c.get("title") == "Radiators (Rointe)")
    assert radiators["entities"] == ["climate.a", "climate.b"]
    # The history graph uses the two mixing instruments.
    graph = next(c for c in heating["cards"] if c["type"] == "history-graph")
    assert len(graph["entities"]) == 2
    # Fans view exists (fan helpers + mapped master present).
    assert fans["title"] == "Fans"


def test_missing_helpers_are_dropped_not_broken():
    config = build_config({}, {})
    # No helpers, no mapped hardware: a single (possibly empty) heating view,
    # with no cards referencing unknown entities.
    heating = config["views"][0]
    for card in heating["cards"]:
        assert card["entities"]  # never an empty entities list


def test_create_dashboards_fails_soft_without_lovelace():
    ctrl, hass = make_controller()
    hass.data = {}  # no lovelace loaded (and the stub notifier is a no-op)
    run(ctrl.async_create_dashboards())  # must not raise
