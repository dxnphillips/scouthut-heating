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

The **one** exception is the final approach (``coast``): a Rointe's oil mass
keeps releasing after its element cuts out, so driving the elements all the way
to target guarantees the room lands above it. When — and only when — the
coordinator has measured that the panels are genuinely hot and the room average
has reached the last fraction of the climb, it passes a coast allowance and the
pushed value is eased below target for a bounded window, so the stored heat lands
the room ON target rather than past it. The staircase underneath is untouched, so
withdrawing the allowance restores the full drive immediately.
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
    probe_moved: bool = True,
    near_target: bool = False,
    coast: float = 0.0,
) -> tuple[float, float, bool, bool, bool]:
    """Return ``(pushed_setpoint, new_stair, evaluated, frozen_held, approach_held)``.

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
    probe_moved: did this heater's probe change AT ALL since the last evaluated
        step? Response-keyed anti-windup: the Rointe floor probe freeze-then-jumps
        through the cloud (reads flat while the room really warms, then leaps a
        couple of degrees in one tick). A flat reading looks "still below target"
        every step, so the plain staircase keeps escalating the overdrive and the
        room sails past target the instant the probe unfreezes. So an *existing*
        overdrive (``prev_stair > 0``) is only escalated when the probe actually
        moved — no movement means no evidence the last step landed, so hold rather
        than wind up. Defaults True (no history → behave as before).
    near_target: has the ZONE AS A WHOLE (its average room temperature, not this
        one laggy per-heater probe) reached within a step of target? Soften-final-
        approach anti-overshoot: during a fast climb the per-heater probes lag the
        real room (cloud lag + 0.5 °C freeze-creep), so they read "still short" and
        the staircase keeps *escalating* the overdrive even after the room has
        actually arrived — then the room sails past target as they catch up (field
        2026-09-14: room averaged 20.25, comfort 19, yet the drive stepped to +1.5).
        So an *existing* overdrive (``prev_stair > 0``) is not escalated once the
        room average is at the top of the approach: hold what is already committed
        and let the mass coast it in. Like the freeze-guard it only ever *withholds
        extra* overdrive (never reduces drive, never blocks the initial climb from
        ``prev_stair <= 0``), so it cannot cause a cold arrival — on a genuine cold
        climb the average is far below target and this never engages. Defaults False.
    coast: °C the pushed setpoint may be eased BELOW target for the final approach,
        because the radiator mass is holding enough stored heat to land the room on
        target by itself. 0 (the default) keeps the old "never below target" rule.
        The coordinator owns every condition — the panels are measurably hot, the
        room average has reached within this allowance of target, and the easing is
        time-boxed — and simply passes the allowance in on the ticks it applies.
        The Rointes fire to their OWN probes, so pushing ``target - coast`` stops
        the heaters whose probe has effectively arrived while the genuinely colder
        end keeps firing: the stored panel heat then carries the last fraction
        instead of the elements pushing through it and the mass landing on top
        (field 2026-09-17: +1.62 °C over target for 111 min of a 150-min booking).
        **The staircase is deliberately left intact underneath**, so the instant
        the coordinator stops passing a coast the pushed value springs straight
        back to ``target + trim`` — recovery is one 30-s tick, not a staircase
        climb. While easing, an existing overdrive is not escalated either (the
        same withhold-only rule as ``near_target``).

    ``evaluated`` is True when the step interval had elapsed and a step decision
    was taken (stepped, held on target, or an up-step was suppressed as a freeze or
    approach hold); the coordinator resets its per-heater step timer on that.
    ``frozen_held`` is True when a would-be up-step was suppressed because the probe
    had not moved; ``approach_held`` when it was suppressed because the room average
    had reached the top of the approach — both surfaced for diagnostics/audit. The
    pushed setpoint is clamped to ``[target, cap]`` and quantised to the 0.5 °C step.
    """
    headroom = max(0.0, cap - target)
    # Head-start bounded to a single step, so it cannot overshoot a non-drooping
    # heater by more than the probe's 0.5 °C resolution.
    ff = min(feedforward(target, outdoor, heatloss_frac), STEP, headroom)
    error = target - probe

    stair = prev_stair
    evaluated = minutes_since_step >= STEP_INTERVAL_MIN
    frozen_held = False
    approach_held = False
    if evaluated:
        if error >= STEP:  # a full 0.5 °C step (or more) below target: nudge up
            # The initial climb to comfort (prev_stair <= 0) is sacred and always
            # steps. An *existing* overdrive is only escalated when BOTH guards are
            # clear: the probe responded to the last step (freeze-guard) AND the
            # room as a whole has not yet reached the top of the approach
            # (soften-final-approach). Each only ever *withholds extra* overdrive —
            # never blocks the climb or a step-down — so neither can leave the room
            # short of comfort; they can only reduce overshoot.
            # The approach hold is judged first: a heater that is BOTH frozen and
            # in a zone that has arrived is reported as approach-held (the outcome
            # — hold — is the same; only the diagnostic differs, but "approach_held
            # empty" must mean the guard was not needed, not that a freeze masked
            # it — field 2026-09-16).
            if prev_stair <= 0:
                stair = prev_stair + STEP
            elif near_target or coast > 0:
                approach_held = True
            elif not probe_moved:
                frozen_held = True
            else:
                stair = prev_stair + STEP
        elif error <= -STEP:  # a full step over target: ease down
            stair = prev_stair - STEP
        # within one step of target: hold (the interval still counts as used)

    # Anti-windup / never below target: keep ff + stair inside [0, headroom].
    stair = max(-ff, min(headroom - ff, stair))
    trim = ff + stair
    if coast > 0:
        # Final approach on stored heat: ease the pushed setpoint BELOW target so
        # the elements cut out and the hot panel mass lands the last fraction. The
        # stair above is left untouched, so dropping the coast restores the full
        # committed drive on the very next tick.
        trim = -coast
    # Clamp AFTER quantising, so rounding can never nudge the pushed value back
    # above the safety cap (harmless while every input sits on the 0.5 grid, but
    # this keeps the cap a hard bound even if a slider is ever off-grid).
    pushed = min(_quantise(target + trim), cap)
    return pushed, stair, evaluated, frozen_held, approach_held
