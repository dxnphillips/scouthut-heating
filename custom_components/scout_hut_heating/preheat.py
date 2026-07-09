"""Adaptive pre-heat (optimum start) estimation.

Pure functions, free of Home Assistant imports, mirroring fan_logic.py. The
coordinator gathers the live signals (zone temperature, comfort target,
outdoor temperature, the learned warm-up rate) and asks this module how long
the pre-heat lead should be, and how a completed warm-up observation should
update the learned rate.

The model is the classic optimum-start one (BS EN 12098-4 style): lead time is
proportional to the temperature deficit, scaled by a learned minutes-per-degree
warm-up rate, with a margin for cold weather. The rate is learned from real
warm-ups with exponential smoothing, so the building's insulation, the
destratification fans, and the season are all absorbed into the observed
number rather than modelled explicitly.
"""

from __future__ import annotations

# Plausibility clamps for the learned warm-up rate (minutes per °C). A
# lightweight hut warms roughly 2-6 °C/h on electric radiators, i.e. 10-30
# min/°C; the clamps leave room either side without letting one bad sample
# (a door left open, a dead sensor) poison the estimate.
MIN_RATE = 5.0
MAX_RATE = 60.0

# Weight of a new observation in the exponential moving average. 0.3 means the
# estimate settles after a handful of bookings but still tracks the seasons.
ALPHA = 0.3

# Ignore warm-up samples with less rise than this (°C): the Rointe room
# readings are coarse and cloud-lagged, so small rises are mostly noise.
MIN_SAMPLE_RISE = 1.0

# Never start later than this many minutes before an event, however warm the
# room already is — the calendar look-ahead needs some window to see events.
MIN_LEAD = 15.0

# Cold-weather margin: +1% lead per °C the outside is below this base. At the
# UK degree-day base (15.5 °C) there is no margin; at -5 °C outside it adds
# ~20%, reflecting the fabric loss the radiators must overcome while raising
# the room.
OUTDOOR_BASE = 15.0
OUTDOOR_MARGIN_PER_DEG = 0.01


def required_lead_minutes(
    *,
    rate: float,
    indoor: float | None,
    target: float,
    outdoor: float | None,
    max_minutes: float,
) -> float:
    """Minutes of pre-heat needed to bring ``indoor`` up to ``target``.

    Falls back to ``max_minutes`` when the room temperature is unknown (a cold
    start must not be missed because the cloud is down). Clamped to
    [MIN_LEAD, max_minutes] — the user's pre-heat slider is the safety cap.
    """
    if indoor is None:
        return max_minutes
    deficit = target - indoor
    if deficit <= 0:
        return min(MIN_LEAD, max_minutes)
    minutes = rate * deficit
    if outdoor is not None and outdoor < OUTDOOR_BASE:
        minutes *= 1 + (OUTDOOR_BASE - outdoor) * OUTDOOR_MARGIN_PER_DEG
    return max(min(minutes, max_minutes), min(MIN_LEAD, max_minutes))


def updated_rate(rate: float, minutes_elapsed: float, temp_rise: float) -> float:
    """Fold one observed warm-up into the learned rate (EWMA).

    ``minutes_elapsed`` over ``temp_rise`` is the observed minutes-per-degree.
    Samples with too little rise are ignored; observations and the result are
    clamped into the plausible band so a single pathological warm-up (opening
    held open, sensor frozen) cannot poison the estimate.
    """
    if temp_rise < MIN_SAMPLE_RISE or minutes_elapsed <= 0:
        return rate
    observed = minutes_elapsed / temp_rise
    observed = max(MIN_RATE, min(MAX_RATE, observed))
    new = rate + ALPHA * (observed - rate)
    return max(MIN_RATE, min(MAX_RATE, new))
