"""The outdoor temperature: a fallback when the weather entity goes quiet, and
an alert when nothing reports one (v1.51.1).

Field 2026-11-03: the configured weather entity gave no temperature from about
04:00Z. From then every cool-off was rejected ``no_outdoor`` (41 in a row by
the 11-04 13:35Z export), every warm-up sample would have read ``mild`` and
been dropped, and every pre-heat was sized for 5 °C outside. Nothing said so.
"""

import pytest

from custom_components.scout_hut_heating.coordinator import OUTDOOR_LOST_ALERT_MINUTES
from scout_testkit import E, advance, make_controller, run

WEATHER = E["weather"]
MET = "weather.forecast_home"


def _events(ctrl, name):
    return [e for e in ctrl.audit.to_list() if e.get("event") == name]


def _watch(ctrl):
    run(ctrl._update_outdoor_watch())


def test_the_configured_weather_is_read_while_it_reports():
    ctrl, hass = make_controller()
    hass.states.set(WEATHER, "cloudy", {"temperature": 7.0})
    hass.states.set(MET, "cloudy", {"temperature": 9.0})
    assert ctrl._outdoor_source() == WEATHER
    assert ctrl._outdoor_temp() == 7.0
    _watch(ctrl)
    assert not _events(ctrl, "outdoor_fallback")


def test_another_weather_entity_carries_the_reading_when_it_goes_quiet():
    ctrl, hass = make_controller()
    hass.states.set(WEATHER, "unavailable", {})
    hass.states.set("weather.elsewhere", "sunny", {"temperature": 3.0})
    hass.states.set(MET, "cloudy", {"temperature": 6.5})
    assert ctrl._outdoor_source() == MET  # Home Assistant's own first
    assert ctrl._outdoor_temp() == 6.5
    _watch(ctrl)
    (evt,) = _events(ctrl, "outdoor_fallback")
    assert (evt["weather"], evt["source"], evt["outdoor"]) == (WEATHER, MET, 6.5)
    _watch(ctrl)
    assert len(_events(ctrl, "outdoor_fallback")) == 1  # once per change
    hass.states.set(WEATHER, "cloudy", {"temperature": 7.0})
    _watch(ctrl)
    (back,) = _events(ctrl, "outdoor_restored")
    assert back["outdoor"] == 7.0
    assert ctrl._outdoor_missing_since is None


def test_a_fahrenheit_fallback_is_converted():
    ctrl, hass = make_controller()
    hass.states.set(MET, "cloudy", {"temperature": 41.0, "temperature_unit": "°F"})
    assert ctrl._outdoor_temp() == pytest.approx(5.0)


def test_with_nothing_reporting_it_is_alerted_after_an_hour_once():
    ctrl, hass = make_controller()
    hass.states.set(WEATHER, "cloudy", {})  # alive, but no temperature
    pushes = []

    async def _push(title, message, **kw):
        pushes.append(message)

    ctrl._push_companion = _push
    _watch(ctrl)
    advance(ctrl, OUTDOOR_LOST_ALERT_MINUTES - 1)
    _watch(ctrl)
    assert not _events(ctrl, "outdoor_lost") and not _events(ctrl, "outdoor_fallback")
    advance(ctrl, 2)
    _watch(ctrl)
    (evt,) = _events(ctrl, "outdoor_lost")
    assert evt["weather"] == WEATHER and evt["minutes"] >= OUTDOOR_LOST_ALERT_MINUTES
    assert len(pushes) == 1 and "5 °C" in pushes[0]
    advance(ctrl, 30)
    _watch(ctrl)
    assert len(_events(ctrl, "outdoor_lost")) == 1 and len(pushes) == 1
    hass.states.set(WEATHER, "cloudy", {"temperature": 4.0})
    _watch(ctrl)
    assert len(_events(ctrl, "outdoor_restored")) == 1
    assert ctrl._outdoor_lost_alerted is False


def test_a_short_gap_leaves_no_trace():
    # The weather entity still loading at a restart, or an API blip.
    ctrl, hass = make_controller()
    _watch(ctrl)
    advance(ctrl, 10)
    hass.states.set(WEATHER, "cloudy", {"temperature": 8.0})
    _watch(ctrl)
    assert not [
        e for e in ctrl.audit.to_list() if str(e.get("event", "")).startswith("outdoor_")
    ]


def test_the_export_says_where_the_reading_comes_from():
    ctrl, hass = make_controller()
    hass.states.set(WEATHER, "unavailable", {})
    hass.states.set(MET, "rainy", {"temperature": 6.0})
    _watch(ctrl)
    data = ctrl.diagnostics_data()
    assert data["state"]["outdoor"]["source"] == MET
    assert data["readings"]["outdoor"] == 6.0
