"""One broken step must not silently take the rest of the reconcile with it.

Field 2026-09-17: something in the back half of ``async_reconcile`` threw for
**4 h 41 min** across two heated evening bookings. Everything up to the drive kept
running — the audit log is full of presets, staircase steps and coast events
through the whole window — but ``_reconcile_fans`` never ran again, so the ceiling
fans stayed stuck in the cooling (forward, down-air) direction while the hall was
being heated, and ``_sample_trace`` never ran either, so the instrument went dark
over exactly the episode that would have explained it. Nothing alerted, and the
only evidence was a hole in the trace.

These tests pin the two properties that would have changed that: the steps after a
failure still run, and the failure is loud.
"""

from custom_components.scout_hut_heating.coordinator import (
    NOTIFY_RECONCILE_ERROR,
)
from scout_testkit import make_controller, run


def _events(ctrl, name):
    return [e for e in ctrl.audit.to_list() if e.get("event") == name]


def _break(ctrl, method: str, exc: Exception | None = None):
    """Make one reconcile step raise, as the field failure did."""
    err = exc or RuntimeError("boom")

    def _raise(*_a, **_k):
        raise err

    setattr(ctrl, method, _raise)
    return err


def test_a_failing_step_does_not_stop_the_later_ones():
    ctrl, _hass = make_controller()
    _break(ctrl, "_reconcile_water")
    seen: list[str] = []
    for later in ("_reconcile_fans", "_sample_trace", "_expire_boosts"):
        original = getattr(ctrl, later)

        def _spy(*a, _n=later, _f=original, **k):
            seen.append(_n)
            return _f(*a, **k)

        setattr(ctrl, later, _spy)

    run(ctrl.async_reconcile())

    # The fans, the trace and boost expiry all sit AFTER water in the order; under
    # the old straight-line try they were abandoned for as long as water threw.
    assert seen == ["_reconcile_fans", "_sample_trace", "_expire_boosts"]


def test_the_trace_keeps_sampling_through_a_broken_step():
    ctrl, _hass = make_controller()
    _break(ctrl, "_reconcile_fans")
    run(ctrl.async_reconcile())
    assert ctrl.trace.to_list(), "the instrument must not go dark"


def test_the_failure_is_audited_and_notified_once_per_episode():
    created: list[dict] = []
    ctrl, _hass = make_controller()
    import custom_components.scout_hut_heating.coordinator as C

    original = C.persistent_notification.async_create
    C.persistent_notification.async_create = lambda hass, msg, **k: created.append(
        {"message": msg, **k}
    )
    try:
        _break(ctrl, "_reconcile_fans", ValueError("bad reading"))
        run(ctrl.async_reconcile())
        run(ctrl.async_reconcile())
        run(ctrl.async_reconcile())
    finally:
        C.persistent_notification.async_create = original

    errors = _events(ctrl, "reconcile_error")
    # Rising edge only: a step that throws every 30 s must not fill the bounded
    # event log (which IS the instrument) or spam the phone.
    assert len(errors) == 1
    assert errors[0]["step"] == "fans"
    assert errors[0]["error"] == "ValueError"
    assert "bad reading" in errors[0]["detail"]
    assert len(created) == 1
    assert created[0]["notification_id"] == NOTIFY_RECONCILE_ERROR
    assert "fans" in created[0]["message"]
    assert ctrl.diagnostics_data()["state"]["reconcile_failing"] == ["fans"]


def test_recovery_clears_the_alert():
    dismissed: list[str] = []
    ctrl, _hass = make_controller()
    import custom_components.scout_hut_heating.coordinator as C

    original = C.persistent_notification.async_dismiss
    C.persistent_notification.async_dismiss = lambda hass, nid: dismissed.append(nid)
    try:
        broken = ctrl._reconcile_fans
        _break(ctrl, "_reconcile_fans")
        run(ctrl.async_reconcile())
        assert ctrl.diagnostics_data()["state"]["reconcile_failing"] == ["fans"]
        ctrl._reconcile_fans = broken  # whatever it was heals
        run(ctrl.async_reconcile())
    finally:
        C.persistent_notification.async_dismiss = original

    assert [e["step"] for e in _events(ctrl, "reconcile_recovered")] == ["fans"]
    assert ctrl.diagnostics_data()["state"]["reconcile_failing"] == []
    assert NOTIFY_RECONCILE_ERROR in dismissed


def test_a_broken_notification_path_cannot_stop_the_reconcile():
    # The alerting is best-effort: it must never become the thing that halts the
    # loop it exists to report on.
    ctrl, _hass = make_controller()
    _break(ctrl, "_reconcile_water")
    _break(ctrl, "_push_companion")
    run(ctrl.async_reconcile())
    assert ctrl.trace.to_list()


def test_nothing_is_recorded_when_every_step_is_healthy():
    ctrl, _hass = make_controller()
    run(ctrl.async_reconcile())
    assert _events(ctrl, "reconcile_error") == []
    assert _events(ctrl, "reconcile_recovered") == []
    assert ctrl.diagnostics_data()["state"]["reconcile_failing"] == []
