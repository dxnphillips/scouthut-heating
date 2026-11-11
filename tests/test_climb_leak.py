"""The climb is charged the leak its gain was learned with (v1.51.3).

Field 2026-11-09: a weekend of overnight cool-offs in a cold-soaked, empty hut
took the hall's heat-loss constant 10.0 -> 5.4 %/h, the rooms' to 4-7. That is
the slow phase, right for how an idle room cools overnight, but the gross gain
was fitted against climbs that stored k 8.7 beside them. Charging 5.4 against
that gain cut an 11 -> 19 lead by ~30 min with nothing about the heating
changed; the 2026-11-04 climb took 207 min.
"""

import pytest

from custom_components.scout_hut_heating.preheat import (
    preheat_start_temp,
    required_lead_minutes,
)
from scout_testkit import E, ZA, make_controller

BASE = dict(rate=16.74, indoor=11.0, target=19.0, outdoor=5.0, max_minutes=240, tail=1.0)


def test_the_climb_leak_is_separate_from_the_idle_cooling():
    stale = required_lead_minutes(cool_k=0.054, **BASE)
    paired = required_lead_minutes(cool_k=0.054, climb_k=0.087, **BASE)
    assert paired - stale >= 15
    assert paired == pytest.approx(required_lead_minutes(cool_k=0.087, **BASE))
    # Idle cooling before the pre-heat still uses the live k.
    far = dict(BASE, gap_hours=8)
    a = preheat_start_temp(cool_k=0.054, climb_k=0.087, **far)
    b = preheat_start_temp(cool_k=0.087, climb_k=0.087, **far)
    assert a > b


def test_the_lead_uses_the_k_stored_with_the_learned_climbs():
    ctrl, hass = make_controller()
    for key, value in (
        ("preheat_minutes", 240),
        ("hall_comfort_temp", 19.0),
        ("zone_a_heatloss_pct", 5.38),
        ("zone_a_warmup_rate", 16.74),
        ("zone_a_warmup_rate_fans", 16.74),
    ):
        ctrl._numbers[key].native_value = value
    hass.states.set(E["weather"], "cloudy", {"temperature": 5.0})
    for climate in E["hall"]:
        hass.states.set(climate, "heat", {"current_temperature": 11.0})
    without = ctrl._zone_preheat_minutes(ZA)
    _, key = ctrl._prediction_rate(ZA)
    ctrl._warmup_history[key] = [(149.6, 5.6, 7.5, 0.087), (207.0, 7.5, 8.0, 0.087)]
    paired = ctrl._zone_preheat_minutes(ZA)
    assert paired - without >= 15
    calc = ctrl._last_lead_calc[ZA]
    assert calc["climb_pct"] == pytest.approx(8.7) and calc["loss_pct"] == 5.38
