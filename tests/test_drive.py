"""Pure 'drive to target' controller (drive.py) — staircase integral + feedforward."""

from custom_components.scout_hut_heating.drive import (
    ESCALATION_BAND,
    STEP,
    STEP_INTERVAL_MIN,
    feedforward,
    update_drive,
)

READY = STEP_INTERVAL_MIN  # a minutes_since_step that lets a step be considered
NOT_YET = STEP_INTERVAL_MIN / 2  # too soon to step


def _drive(probe, outdoor=8.0, target=19.5, cap=24.0, stair=0.0, since=READY, loss=0.09):
    # The existing tests care about (pushed, stair, evaluated); drop the hold flags.
    return update_drive(target, probe, outdoor, loss, cap, stair, since)[:3]


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
    pushed, stair, *_ = update_drive(19.5, 19.5, -20.0, 0.5, 24.0, 0.0, READY)
    assert pushed - 19.5 <= STEP + 1e-9


def test_on_target_holds_plain_target_without_feedforward():
    pushed, *_ = update_drive(19.5, 19.5, None, 0.09, 24.0, 0.0, READY)
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
        pushed, stair, evaluated, *_ = update_drive(
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


# --- Freeze-guard (response-keyed anti-windup) --------------------------------
def test_frozen_probe_holds_the_overdrive_instead_of_escalating():
    # Already overdriving (stair 1.0), probe a full step short, but the probe has
    # NOT moved since the last step -> a stuck reading. Must hold, not wind up.
    pushed, stair, evaluated, frozen, approach = update_drive(
        19.0, 18.0, 8.0, 0.09, 24.0, 1.0, READY, probe_moved=False
    )
    assert evaluated is True
    assert frozen is True
    assert approach is False
    assert stair == 1.0  # held, NOT escalated to 1.5


def test_moved_probe_still_escalates_normally():
    # Same situation but the probe DID move since the last step -> genuine response,
    # so the staircase escalates as before.
    _, stair, _, frozen, _ = update_drive(
        19.0, 18.0, 8.0, 0.09, 24.0, 1.0, READY, probe_moved=True
    )
    assert frozen is False
    assert stair == 1.5


def test_freeze_guard_never_blocks_the_initial_climb_to_comfort():
    # No overdrive committed yet (stair 0): the first step toward comfort is
    # sacred even if the probe reads flat, so it steps up regardless — judged
    # inside the escalation band (v1.50.0), where a step can act.
    _, stair, _, frozen, _ = update_drive(
        19.0, 18.0, 8.0, 0.09, 24.0, 0.0, READY, probe_moved=False
    )
    assert frozen is False
    assert stair == STEP


# --- Escalation band: a step is only taken where it can act (v1.50.0) ----------
def test_no_step_while_the_element_is_saturated():
    # Probe 3.5 short: the element is flat out whatever we push, so a step could
    # only move where it later cuts. Nothing is decided — the interval is NOT
    # consumed (evaluated False) and nothing is reported as held.
    pushed, stair, evaluated, frozen, approach = update_drive(
        19.0, 15.5, None, 0.0, 24.0, 0.0, READY
    )
    assert evaluated is False
    assert stair == 0.0
    assert frozen is False and approach is False
    assert pushed == 19.0


def test_the_band_edge_admits_a_step():
    # The probes move in 0.5 quanta: errors of 0.5 and 1.0 are inside the band,
    # 1.5 is outside it.
    assert ESCALATION_BAND == 1.0
    _, stair, evaluated = _drive(18.5, target=19.5)  # 1.0 short
    assert evaluated is True and stair == STEP
    _, stair, evaluated = _drive(18.0, target=19.5)  # 1.5 short
    assert evaluated is False and stair == 0.0


def test_a_committed_overdrive_is_kept_through_a_cold_spell():
    # A door opening drops the probe well below the band after the droop
    # compensation has landed: the stair is held as it is, not unwound (the
    # room is short, pushing less would be wrong) and not escalated either.
    pushed, stair, evaluated, *_ = update_drive(
        19.0, 17.0, None, 0.0, 24.0, 0.5, READY, probe_moved=True
    )
    assert evaluated is False
    assert stair == 0.5
    assert pushed == 19.5


def test_the_2026_10_12_climb_lands_at_most_one_step_over():
    # Replay of the first session on v1.49.4: probes 15.0, flat at 15.5 for two
    # intervals, then a one-reading leap to 18.5. The old staircase had +1.0
    # committed before the leap (pushed 20.0); now the stair is 0 through the
    # saturated phase and the single droop step lands when the probe arrives.
    stair, since = 0.0, READY
    pushed_max = 0.0
    for probe in (15.0, 15.5, 15.5, 15.5):
        pushed, stair, evaluated, *_ = update_drive(
            19.0, probe, 16.5, 0.08, 23.5, stair, since, probe_moved=(probe == 15.0)
        )
        pushed_max = max(pushed_max, pushed)
        since = 0.0 if evaluated else since + STEP_INTERVAL_MIN
    assert stair == 0.0 and pushed_max == 19.0
    pushed, stair, evaluated, *_ = update_drive(
        19.0, 18.5, 16.5, 0.08, 23.5, stair, since, probe_moved=True
    )
    assert evaluated is True and stair == STEP and pushed == 19.5


def test_reaches_target_from_a_cold_start_on_a_drooping_plant():
    # The band gate must not keep a cold room from comfort: from 4.5 °C short
    # the plant climbs on the plain setpoint, the droop step lands once the probe
    # is in band, and the room still arrives — with no more than a step over.
    final, peak = _simulate(droop=0.7, plant_gain=0.08, start=15.0)
    assert final >= 19.5
    assert peak <= 20.0


def test_freeze_guard_still_allows_step_down_when_over():
    # A frozen probe that reads OVER target must still ease down (the safe
    # direction) — the guard only ever withholds *up*-steps.
    _, stair, _, frozen, _ = update_drive(
        19.0, 20.5, 8.0, 0.09, 24.0, 1.0, READY, probe_moved=False
    )
    assert frozen is False
    assert stair == 0.5


# --- Soften-final-approach (anti-overshoot) -----------------------------------
def test_near_target_holds_the_overdrive_instead_of_escalating():
    # Already overdriving (stair 1.0); this heater's own probe still reads a full
    # step short (laggy), but the ROOM AVERAGE has reached the top of the approach
    # (near_target). Must hold, not wind up — the overshoot the field showed.
    _, stair, evaluated, frozen, approach = update_drive(
        19.0, 18.0, 8.0, 0.09, 24.0, 1.0, READY, probe_moved=True, near_target=True
    )
    assert evaluated is True
    assert approach is True
    assert frozen is False
    assert stair == 1.0  # held, NOT escalated to 1.5


def test_near_target_never_blocks_the_initial_climb():
    # No overdrive committed yet (stair 0): even with the room average near target,
    # the first climb toward comfort is sacred, so it steps up — the brake only ever
    # withholds EXTRA overdrive, so it can never cause a cold arrival.
    _, stair, _, _, approach = update_drive(
        19.0, 18.0, 8.0, 0.09, 24.0, 0.0, READY, probe_moved=True, near_target=True
    )
    assert approach is False
    assert stair == STEP


def test_near_target_still_allows_step_down_when_over():
    # The brake only withholds up-steps: a probe reading a full step over target
    # still eases down even with near_target set.
    _, stair, _, _, approach = update_drive(
        19.0, 20.5, 8.0, 0.09, 24.0, 1.0, READY, probe_moved=True, near_target=True
    )
    assert approach is False
    assert stair == 0.5


def test_far_from_target_escalates_despite_the_flag_being_available():
    # Sanity: with near_target False (a genuine cold climb, room average well below
    # target) the staircase escalates normally — the brake never bites in winter.
    _, stair, _, _, approach = update_drive(
        19.0, 18.0, 8.0, 0.09, 24.0, 1.0, READY, probe_moved=True, near_target=False
    )
    assert approach is False
    assert stair == 1.5


def test_frozen_and_near_target_reports_the_approach_hold():
    # A heater that is BOTH frozen and in a zone that has arrived is reported as
    # approach-held: the outcome (hold) is identical, but "approach_held empty"
    # must mean the guard was not needed, not that a freeze masked it (field
    # 2026-09-16: two frozen-low probes, approach_held [] all boost).
    _, stair, _, frozen, approach = update_drive(
        19.0, 18.0, 8.0, 0.09, 24.0, 1.0, READY, probe_moved=False, near_target=True
    )
    assert approach is True
    assert frozen is False
    assert stair == 1.0
