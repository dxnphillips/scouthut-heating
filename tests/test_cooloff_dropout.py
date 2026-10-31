"""A cool-off sample waits out a reading that blinks (v1.51.0).

Field 2026-10-26 → 10-31: the Rointe integration dropped every heater for one
poll on six of the six nights (06:28, 00:37, 06:01, 04:24, 01:36, 04:06Z), and
each time v1.50.9 discarded the running hall AND office cool-off sample
(`probes_changed`) and reset the settle clock, so the next sample anchored
"20 min after icing" when the zone had been on ice for hours. The overnight
samples are the slow, honest ones the heat-loss constant needs.
"""

import custom_components.scout_hut_heating.coordinator as C
from custom_components.scout_hut_heating.const import CONF_HALL_CLIMATES
from scout_testkit import ZA, ZB, E, PRESET_COMFORT, PRESET_ICE, advance, make_controller

FRONT, BACK, LEFT, RIGHT = (
    "climate.hall_front",
    "climate.hall_back",
    "climate.hall_left",
    "climate.hall_right",
)
HALL = [FRONT, BACK, LEFT, RIGHT]


def _set(hass, readings):
    for climate, temp in readings.items():
        if temp is None:
            hass.states.set(climate, "unavailable", {})
        else:
            hass.states.set(climate, "heat", {"current_temperature": temp})


def _events(ctrl, name):
    return [e for e in ctrl.audit.to_list() if e.get("event") == name]


def _all(temp):
    return dict.fromkeys(HALL, temp)


def _sampling():
    """A hall on ice for hours with a cool-off sample running."""
    ctrl, hass = make_controller({CONF_HALL_CLIMATES: list(HALL)})
    hass.states.set("weather.forecast", "cloudy", {"temperature": 8.0})
    ctrl._numbers["zone_a_heatloss_pct"].native_value = 12.0
    _set(hass, _all(15.0))
    ctrl.applied[ZA] = PRESET_ICE
    ctrl._update_cooloff_learning()  # the settle clock starts
    advance(ctrl, 180)
    ctrl._update_cooloff_learning()  # anchors, three hours after icing
    assert ctrl._cooloff_start[ZA] is not None
    return ctrl, hass


def test_a_thirty_second_dropout_keeps_the_sample():
    ctrl, hass = _sampling()
    started = ctrl._cooloff_start[ZA][0]
    advance(ctrl, 60)
    _set(hass, _all(None))  # every heater gone for one poll
    ctrl._update_cooloff_learning()
    advance(ctrl, 0.5)
    _set(hass, _all(14.5))
    ctrl._update_cooloff_learning()
    assert not _events(ctrl, "cooloff_discarded")
    assert ctrl._cooloff_start[ZA][0] == started - C.timedelta(minutes=60.5)
    assert ctrl._cooloff_start[ZA][1] == 15.0
    assert ZA not in ctrl._cooloff_dropout_since


def test_one_probe_blinking_keeps_the_sample():
    # 2026-10-31 04:06Z: part of the hall dropped, so the floor was not lost but
    # the set of probes changed twice in 30 s.
    ctrl, hass = _sampling()
    advance(ctrl, 60)
    _set(hass, {LEFT: None})
    ctrl._update_cooloff_learning()
    advance(ctrl, 0.5)
    _set(hass, {LEFT: 15.0})
    ctrl._update_cooloff_learning()
    assert not _events(ctrl, "cooloff_discarded")
    assert ctrl._cooloff_start[ZA][1] == 15.0


def test_the_ticks_during_the_blink_are_not_counted():
    ctrl, hass = _sampling()
    for _ in range(2):
        advance(ctrl, 0.5)
        ctrl._update_cooloff_learning()
    ticks = ctrl._cooloff_start[ZA][5]
    assert ticks == 3
    _set(hass, {LEFT: None})
    advance(ctrl, 0.5)
    ctrl._update_cooloff_learning()
    assert ctrl._cooloff_start[ZA][5] == ticks


def test_a_reading_lost_past_the_grace_ends_the_sample():
    ctrl, hass = _sampling()
    _set(hass, _all(None))
    ctrl._update_cooloff_learning()
    advance(ctrl, C.COOL_DROPOUT_GRACE_MIN + 1)
    ctrl._update_cooloff_learning()
    (gone,) = _events(ctrl, "cooloff_discarded")
    assert gone["reason"] == "reading_lost"
    assert ctrl._cooloff_start[ZA] is None


def test_a_plausibility_swap_still_ends_it_at_once():
    ctrl, hass = _sampling()
    _set(hass, {FRONT: 15.0, BACK: 15.0, LEFT: 10.0, RIGHT: 15.0})  # LEFT now insane
    ctrl._update_cooloff_learning()
    (gone,) = _events(ctrl, "cooloff_discarded")
    assert gone["reason"] == "probes_changed"


def test_heating_resuming_during_a_blink_ends_the_sample():
    ctrl, hass = _sampling()
    _set(hass, _all(None))
    ctrl.applied[ZA] = PRESET_COMFORT
    ctrl._update_cooloff_learning()
    assert ctrl._cooloff_start[ZA] is None


def test_the_settle_clock_survives_a_blink():
    # 2026-10-30 04:18Z: a sample anchored after the 01:36Z dropout recorded
    # `settled_min` 20.0 when the hall had been on ice since 22:57Z.
    ctrl, hass = make_controller({CONF_HALL_CLIMATES: list(HALL)})
    hass.states.set("weather.forecast", "cloudy", {"temperature": 8.0})
    _set(hass, _all(15.0))
    ctrl.applied[ZA] = PRESET_ICE
    ctrl._update_cooloff_learning()
    since = ctrl._cooloff_cooling_since[ZA]
    advance(ctrl, 10)
    _set(hass, _all(None))
    ctrl._update_cooloff_learning()
    assert ctrl._cooloff_cooling_since[ZA] == since - C.timedelta(minutes=10)
    _set(hass, _all(15.0))
    advance(ctrl, C.COOL_SETTLE_MINUTES - 9)
    ctrl._update_cooloff_learning()
    assert ctrl._cooloff_start[ZA] is not None  # 21 min after icing, not 11 after the blink


def test_heating_still_resets_the_settle_clock():
    ctrl, hass = make_controller({CONF_HALL_CLIMATES: list(HALL)})
    _set(hass, _all(15.0))
    ctrl.applied[ZA] = PRESET_ICE
    ctrl._update_cooloff_learning()
    ctrl.applied[ZA] = PRESET_COMFORT
    ctrl._update_cooloff_learning()
    assert ctrl._cooloff_cooling_since[ZA] is None


def test_the_office_waits_out_its_blink_too():
    ctrl, hass = make_controller()
    hass.states.set("weather.forecast", "cloudy", {"temperature": 8.0})
    for eid in E["office"]:
        hass.states.set(eid, "heat", {"current_temperature": 16.0})
    ctrl.applied[ZB] = PRESET_ICE
    ctrl._update_cooloff_learning()
    advance(ctrl, 180)
    ctrl._update_cooloff_learning()
    assert ctrl._cooloff_start[ZB] is not None
    for eid in E["office"]:
        hass.states.set(eid, "unavailable", {})
    ctrl._update_cooloff_learning()
    for eid in E["office"]:
        hass.states.set(eid, "heat", {"current_temperature": 16.0})
    advance(ctrl, 0.5)
    ctrl._update_cooloff_learning()
    assert not [e for e in _events(ctrl, "cooloff_discarded") if e["zone"] == ZB]
    assert ctrl._cooloff_start[ZB] is not None
