"""Pure 'drive to target' controller (drive.py) — staircase integral + feedforward."""

from custom_components.scout_hut_heating.drive import (
    FF_GAIN,
    STEP,
    STEP_INTERVAL_MIN,
    feedforward,
    update_drive,
)

READY = STEP_INTERVAL_MIN  # a minutes_since_step that lets a step be considered
NOT_YET = STEP_INTERVAL_MIN / 2  # too soon to step


def _drive(probe, outdoor=8.0, target=19.5, cap=24.0, stair=0.0, since=READY, loss=0.09):
    return update_drive(target, probe, outdoor, loss, cap, stair, since)


# --- Feedforward ---------------------------------------------------------------
def test_feedforward_scales_with_cold():
    assert feedforward(19.5, 5.0, 0.09) > feedforward(19.5, 15.0, 0.09) > 0


def test_feedforward_zero_without_outdoor_or_loss():
    assert feedforward(19.5, None, 0.09) == 0.0
    assert feedforward(19.5, 5.0, 0.0) == 0.0


def test_feedforward_never_negative_when_warmer_outside():
    assert feedforward(19.5, 25.0, 0.09) == 0.0


# --- Head-start is bounded to one step ----------------------------------------
def test_head_start_capped_at_one_step_even_when_very_cold():
    # Deep cold: raw feedforward would exceed a step; it must be clamped to STEP.
    pushed, stair, _ = update_drive(19.5, 19.5, -20.0, 0.5, 24.0, 0.0, READY)
    assert pushed - 19.5 <= STEP + 1e-9


def test_on_target_holds_plain_target_without_feedforward():
    pushed, _, _ = update_drive(19.5, 19.5, None, 0.09, 24.0, 0.0, READY)
    assert pushed == 19.5  # no outdoor -> no head-start -> just target


# --- Staircase timing ----------------------------------------------------------
def test_does_not_step_before_the_interval():
    pushed, stair, evaluated = _drive(18.0, stair=0.0, since=NOT_YET)
    assert evaluated is False
    assert stair == 0.0  # no step taken yet


def test_steps_up_one_step_when_short_after_interval():
    _, stair, evaluated = _drive(18.5, stair=0.0, since=READY)  # 1.0 short
    assert evaluated is True
    assert stair == STEP


def test_steps_down_when_over_after_interval():
    _, stair, _ = _drive(20.5, stair=1.0, since=READY)  # 1.0 over
    assert stair == 0.5  # eased one step


def test_holds_within_one_step_of_target():
    # Probe 0.25 under target: within a step, no nudge.
    _, stair, evaluated = _drive(19.25, stair=0.5, since=READY)
    assert evaluated is True
    assert stair == 0.5


# --- Never below target, cap, quantise ----------------------------------------
def test_never_pushes_below_target():
    pushed, _, _ = _drive(23.0, stair=2.0, since=READY)  # very hot
    assert pushed >= 19.5


def test_clamped_to_cap():
    pushed, _, _ = _drive(10.0, stair=50.0, since=READY, cap=24.0)
    assert pushed == 24.0


def test_output_quantised_to_the_step():
    for probe in (18.3, 18.7, 19.1):
        pushed, _, _ = _drive(probe, stair=0.3)
        assert abs((pushed / STEP) - round(pushed / STEP)) < 1e-9


# --- Closed-loop behaviour on a simulated slow, drooping plant -----------------
def _simulate(droop, plant_gain, *, ticks=1200, start=18.0, outdoor=6.0, cap=24.0):
    """Run the controller against a first-order plant that settles ``droop`` °C
    below whatever setpoint is pushed (the Rointe behaviour we are cancelling).
    Returns (final_probe, peak_probe)."""
    target = 19.5
    stair, since, probe_true = 0.0, 0.0, start
    peak = start
    for _ in range(ticks):
        # The controller only ever sees the probe reported in 0.5 °C steps; the
        # true room temperature accumulates continuously underneath.
        probe_meas = round(probe_true * 2) / 2
        pushed, stair, evaluated = update_drive(
            target, probe_meas, outdoor, 0.09, cap, stair, since
        )
        since = 0.0 if evaluated else since + 1.0
        probe_true += plant_gain * ((pushed - droop) - probe_true)
        peak = max(peak, probe_true)
    return round(probe_true * 2) / 2, round(peak * 2) / 2


def test_reaches_target_on_a_drooping_plant():
    final, _ = _simulate(droop=0.7, plant_gain=0.08)
    assert final >= 19.5  # got there (was stuck ~0.7 under without the loop)


def test_does_not_overshoot_a_drooping_plant():
    _, peak = _simulate(droop=0.7, plant_gain=0.08)
    assert peak <= 20.5  # at most ~one step of overshoot, never runs away


def test_no_windup_overshoot_on_a_very_slow_plant():
    # A sluggish plant is where a continuous integral would wind up and overshoot;
    # the wait-between-steps staircase must not.
    final, peak = _simulate(droop=0.7, plant_gain=0.03)
    assert 19.5 <= final
    assert peak <= 20.5


def test_no_droop_plant_settles_at_target_not_above():
    # A heater that already reaches its setpoint must not be driven up and parked.
    final, peak = _simulate(droop=0.0, plant_gain=0.1)
    assert final <= 20.0  # head-start cancelled by the staircase
    assert peak <= 20.5
