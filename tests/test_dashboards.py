"""The generated dashboard config and its graceful failure path."""

from custom_components.scout_hut_heating.dashboards import (
    _existing_views,
    _preserve_foreign,
    build_config,
)
from scout_testkit import make_controller, run


def _titles(view):
    return [c.get("title") for c in view["cards"]]


def test_build_config_resolves_real_entity_ids():
    emap = {
        "zone_a_status": "sensor.x_hall_preset",
        "boost_zone_a": "button.x_boost_hall",
        "hall_comfort_temp": "number.x_hall_comfort",
        "fans_enabled": "switch.x_fans",
        "fan_delta_t": "sensor.x_dt",
        "fan_mix": "sensor.x_mix",
        "hall_temp_spread": "sensor.x_spread",
    }
    mapped = {
        "hall_climates": ["climate.a", "climate.b"],
        "fan_master": "switch.m",
        "ceiling_temp": "sensor.roof",
    }
    config = build_config(emap, mapped)
    home, heating, fans = config["views"]
    # The simple Home view leads, carrying status and the day-to-day actions.
    assert home["title"] == "Home" and home["path"] == "home"
    home_entities = [e for card in home["cards"] for e in card["entities"]]
    assert {"entity": "sensor.x_hall_preset", "name": "Hall"} in home_entities
    assert {
        "entity": "button.x_boost_hall",
        "name": "Boost hall heating",
    } in home_entities
    # Home carries the temperature trend (feels-like + ceiling), not the
    # stratification differences.
    home_graph = next(c for c in home["cards"] if c["type"] == "history-graph")
    assert home_graph["title"] == "Temperatures (24 h)"
    assert {"entity": "sensor.x_mix", "name": "Head-height feels-like"} in home_graph[
        "entities"
    ]
    assert {"entity": "sensor.roof", "name": "Ceiling (roof)"} in home_graph["entities"]
    assert {"entity": "sensor.x_hall_preset", "name": "Hall preset"} in heating[
        "cards"
    ][0]["entities"]
    # The head-height mix temp is surfaced on the fans Status card.
    status = next(c for c in fans["cards"] if c.get("title") == "Status")
    assert {"entity": "sensor.x_mix", "name": "Head-height mix temp"} in status[
        "entities"
    ]
    # Radiators card lists the mapped climates verbatim.
    radiators = next(
        c for c in heating["cards"] if c.get("title") == "Radiators (Rointe)"
    )
    assert radiators["entities"] == ["climate.a", "climate.b"]
    # Heating has BOTH graphs: absolute temps and the stratification differences.
    graphs = {c["title"]: c for c in heating["cards"] if c["type"] == "history-graph"}
    assert set(graphs) == {"Temperatures (24 h)", "Stratification (24 h)"}
    assert len(graphs["Temperatures (24 h)"]["entities"]) == 2  # feels-like + ceiling
    assert len(graphs["Stratification (24 h)"]["entities"]) == 2  # ΔT + spread
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


def test_preserve_foreign_keeps_other_integrations_views():
    # A shared dashboard where another integration (the alarm) added a tab.
    existing = [
        {"title": "Home", "path": "home", "cards": ["stale"]},
        {"title": "Heating", "path": "heating", "cards": ["stale"]},
        {"title": "Fans", "path": "fans", "cards": ["stale"]},
        {"title": "Alarm", "path": "alarm", "cards": ["alarm-card"]},
    ]
    config = {"views": [{"title": "Home", "path": "home", "cards": ["fresh"]}]}
    merged = _preserve_foreign(existing, config)
    paths = [v["path"] for v in merged["views"]]
    # Our fresh view replaces the stale one; the foreign Alarm tab survives.
    assert paths == ["home", "alarm"]
    home = next(v for v in merged["views"] if v["path"] == "home")
    assert home["cards"] == ["fresh"]
    alarm = next(v for v in merged["views"] if v["path"] == "alarm")
    assert alarm["cards"] == ["alarm-card"]
    # The input config is not mutated.
    assert config["views"] == [{"title": "Home", "path": "home", "cards": ["fresh"]}]


def test_preserve_foreign_with_no_existing_views():
    config = {"views": [{"title": "Heating", "path": "heating"}]}
    merged = _preserve_foreign([], config)
    assert merged["views"] == [{"title": "Heating", "path": "heating"}]


class _FakeDashboard:
    """A stand-in Lovelace store whose async_load shape is configurable."""

    def __init__(self, stored, *, accepts_arg=True, raises=None):
        self._stored = stored
        self._accepts_arg = accepts_arg
        self._raises = raises

    async def async_load(self, *args):
        if self._raises is not None:
            raise self._raises
        if args and not self._accepts_arg:
            raise TypeError("async_load() takes no positional arguments")
        return self._stored


def test_existing_views_reads_stored_views():
    dash = _FakeDashboard({"views": [{"path": "alarm"}, {"path": "home"}, "junk"]})
    views = run(_existing_views(dash))
    assert views == [{"path": "alarm"}, {"path": "home"}]  # non-dict dropped


def test_existing_views_handles_no_arg_signature():
    dash = _FakeDashboard({"views": [{"path": "alarm"}]}, accepts_arg=False)
    assert run(_existing_views(dash)) == [{"path": "alarm"}]


def test_existing_views_empty_when_never_saved():
    dash = _FakeDashboard(None, raises=ValueError("no config saved yet"))
    assert run(_existing_views(dash)) == []
