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

# ...and with less duration than this (minutes): a cloud-lagged reading that
# catches up in one jump would otherwise register an implausibly fast warm-up
# and walk the learned rate to the clamp in a couple of incidents.
MIN_SAMPLE_MINUTES = 10.0

# Never start later than this many minutes before an event, however warm the
# room already is — the calendar look-ahead needs some window to see events.
MIN_LEAD = 15.0

# Cold-weather margin: +1% lead per °C the outside is below this base. At the
# UK degree-day base (15.5 °C) there is no margin; at -5 °C outside it adds
# ~20%, reflecting the fabric loss the radiators must overcome while raising
# the room.
OUTDOOR_BASE = 15.0
OUTDOOR_MARGIN_PER_DEG = 0.01

# Cooling prediction never assumes the room drops below the Rointe anti-frost
# floor — the heating system holds it there even when "off".
MIN_PREDICT_TEMP = 7.0

# Plausibility clamps and sample thresholds for the learned heat-loss
# (cool-off) rate, °C per hour. A poorly insulated timber hut plausibly loses
# 0.3-3 °C/h against a cold outside; require a real drop over a real duration
# so cloud noise cannot masquerade as heat loss.
MIN_COOL_RATE = 0.1
MAX_COOL_RATE = 5.0
MIN_COOL_SAMPLE_DROP = 1.0
MIN_COOL_SAMPLE_HOURS = 0.5


def required_lead_minutes(
    *,
    rate: float,
    indoor: float | None,
    target: float,
    outdoor: float | None,
    max_minutes: float,
    gap_hours: float | None = None,
    cool_rate: float = 0.0,
) -> float:
    """Minutes of pre-heat needed to bring the room up to ``target``.

    Falls back to ``max_minutes`` when the room temperature is unknown (a cold
    start must not be missed because the cloud is down). Clamped to
    [MIN_LEAD, max_minutes] — the user's pre-heat slider is the safety cap.

    When the event start is known (``gap_hours`` from now) and a heat-loss
    rate has been learned, the lead accounts for the room continuing to cool
    until the pre-heat actually begins: the deficit is grown by the predicted
    loss over the idle gap, solved in closed form (the loss stops mattering
    once heating starts because the learned warm-up rate already includes
    fabric losses while heating). The prediction never assumes a drop below
    the anti-frost floor.
    """
    if indoor is None:
        return max_minutes
    deficit = target - indoor
    lead = max(rate * deficit, 0.0)
    if gap_hours is not None and gap_hours > 0 and cool_rate > 0 and lead < gap_hours * 60:
        # Idle until the pre-heat begins: deficit grows while the room cools.
        # lead = rate * (deficit + cool_rate * idle_hours), where
        # idle_hours = gap_hours - lead/60 — solved for lead. The divisor is
        # only valid while the room is still cooling when the pre-heat starts;
        # once the anti-frost floor binds, the room has bottomed out long
        # before then and the deficit is simply fixed at (target - floor).
        raw = deficit + cool_rate * gap_hours
        floor_deficit = target - MIN_PREDICT_TEMP
        if raw > deficit:
            if raw <= floor_deficit:
                lead = rate * raw / (1 + rate * cool_rate / 60)
            elif floor_deficit > 0:
                lead = rate * floor_deficit
    if lead <= 0:
        return min(MIN_LEAD, max_minutes)
    if outdoor is not None and outdoor < OUTDOOR_BASE:
        lead *= 1 + (OUTDOOR_BASE - outdoor) * OUTDOOR_MARGIN_PER_DEG
    return max(min(lead, max_minutes), min(MIN_LEAD, max_minutes))


def updated_rate(rate: float, minutes_elapsed: float, temp_rise: float) -> float:
    """Fold one observed warm-up into the learned rate (EWMA).

    ``minutes_elapsed`` over ``temp_rise`` is the observed minutes-per-degree.
    Samples with too little rise are ignored; observations and the result are
    clamped into the plausible band so a single pathological warm-up (opening
    held open, sensor frozen) cannot poison the estimate.
    """
    if temp_rise < MIN_SAMPLE_RISE or minutes_elapsed < MIN_SAMPLE_MINUTES:
        return rate
    observed = minutes_elapsed / temp_rise
    observed = max(MIN_RATE, min(MAX_RATE, observed))
    new = rate + ALPHA * (observed - rate)
    return max(MIN_RATE, min(MAX_RATE, new))


def updated_cooling_rate(rate: float, hours_elapsed: float, temp_drop: float) -> float:
    """Fold one observed cool-off into the learned heat-loss rate (EWMA).

    Samples need a real drop over a real duration; observations and the
    result are clamped so a burst of solar gain or a frozen cloud reading
    cannot poison the estimate.
    """
    if temp_drop < MIN_COOL_SAMPLE_DROP or hours_elapsed < MIN_COOL_SAMPLE_HOURS:
        return rate
    observed = temp_drop / hours_elapsed
    observed = max(MIN_COOL_RATE, min(MAX_COOL_RATE, observed))
    new = rate + ALPHA * (observed - rate)
    return max(MIN_COOL_RATE, min(MAX_COOL_RATE, new))
