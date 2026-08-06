"""Pure 'drive to target' controller — an outer trim loop over one heater.

The Rointe heaters settle a fraction *below* the setpoint we give them: their
own control eases the element as it nears setpoint (and there may be a probe
offset), so on the field data a hall probe held ~0.5 °C under a 19.5 °C comfort
setpoint on a cold night while the integration was told it was "full". Because
the integration OWNS the setpoint it pushes, it can close an outer loop on each
heater's *own* probe and overdrive the setpoint until the probe actually reaches
target — cancelling the Rointe's steady-state droop instead of trusting it.

This module is pure maths, like ``preheat.py`` / ``fan_logic.py``: given the
target, the probe, the outdoor temperature, the learned heat-loss and the
previous trim, it returns the setpoint to push and the new trim. All policy —
which heaters, probe freshness, cross-probe sanity, the fail-safe withdrawal and
the "last will" reset — lives in the coordinator.

Why a *staircase* integral rather than a textbook PID
-----------------------------------------------------
Three facts about this plant rule out a naive PID:

1. **The feedback is quantised to 0.5 °C** (the Rointe probe/step). A derivative
   term would be pure quantisation noise, so there is none.
2. **The plant is slow and variable** — tens of minutes to an hour to respond,
   and we have no trustworthy model of it. A continuous integral would *wind up*
   over the long climb and then overshoot; the room would "go over".
3. **We must not hover under *or* over.**

So the trim is moved like a careful human would: **nudge the setpoint one 0.5 °C
step, wait ``STEP_INTERVAL_MIN`` for the building to respond, then re-check** —
step up if still a full step short, step down if a full step over, hold if on
target. Waiting between steps is the anti-windup: the trim can't run ahead of a
plant that hasn't reacted yet, so overshoot is bounded to about one step. It is
deliberately *patient* (the owner's chosen trade for a slow, leaky hall).

A **feedforward** from the *learned* heat-loss gives the staircase a cold-night
head-start (``FF_GAIN * heatloss_frac * (target - outdoor)``) so it doesn't have
to climb from zero every time. It is kept conservative — it under-estimates the
drive needed, so it never overshoots on its own; the staircase supplies the rest
and can also step *below* the feedforward to cancel it if it proves too eager.

The loop only ever drives **harder** than the owner's setpoint, never softer
(the Rointe backs itself off below target; pushing the setpoint down would just
make the room colder). Output is clamped to ``[target, cap]`` and quantised to
the Rointe's 0.5 °C step, so the coordinator only re-pushes on a real change.
"""

from __future__ import annotations

# Feedforward: °C of head-start boost per unit of (heatloss_frac × indoor-outdoor
# gap). Small on purpose, and separately capped at one STEP below — the head-start
# is only ever a single 0.5 °C nudge, so on a heater that turns out NOT to droop
# the transient overshoot can never exceed the probe's own 0.5 °C resolution.
# The staircase, not the feedforward, carries the real outdoor-dependent drive.
FF_GAIN = 0.2
# The Rointe comfort/eco number entities (and their probes) move in 0.5 °C steps.
STEP = 0.5
# Wait this long between staircase steps, so each 0.5 °C nudge has time to
# express in this slow building before the next is considered. This interval IS
# the anti-windup — shorter risks stepping faster than the hall responds and
# overshooting; longer is safe but slower to settle.
STEP_INTERVAL_MIN = 15.0


def _quantise(value: float, step: float = STEP) -> float:
    return round(value / step) * step


def feedforward(target: float, outdoor: float | None, heatloss_frac: float) -> float:
    """Outdoor-scaled head-start boost from the learned heat-loss constant.

    Zero when outdoor is unknown or heat-loss prediction is disabled — the
    staircase then does all the work rather than the loop guessing.
    """
    if outdoor is None or heatloss_frac <= 0:
        return 0.0
    return FF_GAIN * heatloss_frac * max(0.0, target - outdoor)


def update_drive(
    target: float,
    probe: float,
    outdoor: float | None,
    heatloss_frac: float,
    cap: float,
    prev_stair: float,
    minutes_since_step: float,
) -> tuple[float, float, bool]:
    """Return ``(pushed_setpoint, new_stair, evaluated)`` for one heater.

    target: temperature we want this heater's probe to reach.
    probe:  the heater's own current temperature.
    outdoor: outdoor temperature (feeds the feedforward); ``None`` disables it.
    heatloss_frac: the zone's learned heat-loss as a fraction (``%/100``).
    cap:    absolute maximum setpoint we may push — the safety envelope.
    prev_stair: the staircase term from the last tick (may be negative, to
        cancel an over-eager feedforward; 0 at startup — the coordinator does
        NOT persist it across a restart, so a crash cannot leave a wound-up
        drive behind).
    minutes_since_step: minutes since the staircase was last *evaluated*; a step
        is only considered once this reaches ``STEP_INTERVAL_MIN``.

    ``evaluated`` is True when the step interval had elapsed and a step decision
    was taken (stepped or deliberately held on target); the coordinator resets
    its per-heater step timer on that. The pushed setpoint is clamped to
    ``[target, cap]`` and quantised to the Rointe's 0.5 °C step.
    """
    headroom = max(0.0, cap - target)
    # Head-start bounded to a single step, so it cannot overshoot a non-drooping
    # heater by more than the probe's 0.5 °C resolution.
    ff = min(feedforward(target, outdoor, heatloss_frac), STEP, headroom)
    error = target - probe

    stair = prev_stair
    evaluated = minutes_since_step >= STEP_INTERVAL_MIN
    if evaluated:
        if error >= STEP:  # a full 0.5 °C step (or more) below target: nudge up
            stair = prev_stair + STEP
        elif error <= -STEP:  # a full step over target: ease down
            stair = prev_stair - STEP
        # within one step of target: hold (the interval still counts as used)

    # Anti-windup / never below target: keep ff + stair inside [0, headroom].
    stair = max(-ff, min(headroom - ff, stair))
    trim = ff + stair
    pushed = _quantise(min(target + trim, cap))
    return pushed, stair, evaluated
