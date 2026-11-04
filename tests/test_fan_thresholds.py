"""The fan start/stop lines: new defaults, applied once to an existing install
(v1.51.1).

With the hall empty, unheated and the fans off at 03–05Z, the ceiling read a
median ~0.6 °C above the radiator probes over 27 nights (Oct–Nov 2026). The
owner's sliders sat at 0.5 / 0.0, under that resting gap, so the reverse fans
started on every heating episode (seven starts in one four-hour empty pre-heat
on 2026-11-04, 85 runs in the month) and three of five Pause presses came 1–17
min after a start in an occupied hall. Owner: "set them as the new defaults".
"""

from custom_components.scout_hut_heating.const import NUMBER_DEFS
from custom_components.scout_hut_heating.fan_logic import fan_decision
from scout_testkit import make_controller, run


def _events(ctrl, name):
    return [e for e in ctrl.audit.to_list() if e.get("event") == name]


def _restored(data, dt_on=0.5, dt_off=0.0):
    ctrl, _ = make_controller()
    ctrl._numbers["fan_dt_on"].native_value = dt_on
    ctrl._numbers["fan_dt_off"].native_value = dt_off

    async def _load():
        return data

    ctrl._store.async_load = _load
    run(ctrl._async_restore_state())
    return ctrl


def test_the_defaults_are_one_and_a_half_and_a_half():
    assert NUMBER_DEFS["fan_dt_on"][3] == 1.5
    assert NUMBER_DEFS["fan_dt_off"][3] == 0.5


def test_an_existing_install_moves_onto_them_once():
    ctrl = _restored({"audit": [], "trace": [], "warmup_tail_v": 1})
    assert ctrl.number("fan_dt_on") == 1.5
    assert ctrl.number("fan_dt_off") == 0.5
    (evt,) = _events(ctrl, "fan_defaults_applied")
    assert evt["fan_dt_on"] == [0.5, 1.5] and evt["fan_dt_off"] == [0.0, 0.5]
    assert ctrl._store.saved["fan_dt_v"] == 1  # saved at once
    # A slider moved afterwards is the owner's: a later start leaves it alone.
    again = _restored(ctrl._store.saved, dt_on=1.0, dt_off=0.5)
    assert again.number("fan_dt_on") == 1.0
    assert len(_events(again, "fan_defaults_applied")) == 1  # the saved one, no new one


def test_a_pre_v1_51_store_gets_both_migrations_in_one_save():
    ctrl = _restored({"audit": [], "trace": []})
    assert ctrl.number("fan_dt_on") == 1.5
    assert _events(ctrl, "warmup_gain_converted")
    assert ctrl._store.saved["fan_dt_v"] == 1 and ctrl._store.saved["warmup_tail_v"] == 1


def test_a_fresh_install_has_nothing_to_move():
    ctrl = _restored(None, dt_on=1.5, dt_off=0.5)
    assert not _events(ctrl, "fan_defaults_applied")


def _decide(dt, running):
    on, _, _ = fan_decision(
        summer=False, occupied=False, warm=False, overheated=False, dt=dt,
        dt_on=NUMBER_DEFS["fan_dt_on"][3], dt_off=NUMBER_DEFS["fan_dt_off"][3],
        demand=True, recirc_ok=False, currently_winter=running, run_on_loss=True,
    )
    return on


def test_the_resting_gap_no_longer_starts_them_and_real_pooling_does():
    assert not _decide(0.6, running=False)  # the hall at rest, a pre-heat's first tick
    assert _decide(1.6, running=False)
    assert _decide(0.75, running=True)  # still mixing
    assert not _decide(0.5, running=True)  # back to the resting gap: stop
