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

import math

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

# Learned heat-loss constant k: the FRACTION of the indoor-outdoor gap lost
# per hour (Newton cooling, dT/dt = -k·(indoor - outdoor)). Gap-normalising
# the observation is what makes a July sample transfer to January: the same
# fabric loses ~0.10/h in this hall and ~0.045/h in the insulated office
# (measured July 2026) whatever the season — only the gap changes. Clamps
# span super-insulated to open-barn; samples need a real drop over a real
# duration AND a real gap (k = drop/(hours·gap) explodes near equilibrium,
# where a mild day teaches nothing).
MIN_COOL_K = 0.005
MAX_COOL_K = 0.5
MIN_COOL_SAMPLE_DROP = 1.0
MIN_COOL_SAMPLE_HOURS = 0.5
# Minimum indoor-outdoor gap for a trustworthy sample. Raised 3.0 -> 4.0 on
# 2026-07-18 field evidence: across 36 accepted hall cool-offs, every
# rate-spike (10.3 %/h -> 20.5 %/h, 12.9 -> 20.1) came from a short
# just-vacated-room sample at gap 3.35-3.40 shedding stored heat fast into a
# cool evening; every *reliable* sample sat at gap >= 4. Below ~4 the k =
# drop/(hours*gap) normalisation is too noisy to trust. Rejecting these is
# fail-safe-neutral (it drops noise, it does not clamp a real high-loss
# reading down, which would shorten the lead and risk a cold arrival).
MIN_COOL_SAMPLE_GAP = 4.0

# When the weather entity is unreadable, predict idle-gap cooling against a
# cold-ish outdoor rather than skipping the prediction: err warm, the
# pre-heat cap bounds the damage.
COOL_FALLBACK_OUTDOOR = 5.0


def required_lead_minutes(
    *,
    rate: float,
    indoor: float | None,
    target: float,
    outdoor: float | None,
    max_minutes: float,
    gap_hours: float | None = None,
    cool_k: float = 0.0,
) -> float:
    """Minutes of pre-heat needed to bring the room up to ``target``.

    Falls back to ``max_minutes`` when the room temperature is unknown (a cold
    start must not be missed because the cloud is down). Clamped to
    [MIN_LEAD, max_minutes] — the user's pre-heat slider is the safety cap.

    When the event start is known (``gap_hours`` from now) and a heat-loss
    constant has been learned, the room's temperature at pre-heat time is
    predicted by Newton cooling toward the outdoor temperature, and the lead
    sized for that predicted deficit. Decaying across the FULL gap (rather
    than gap minus the eventual lead) slightly over-predicts the cooling —
    deliberately: the error is a few minutes of extra lead in the warm
    direction, and it keeps the expression closed-form. The prediction never
    assumes a drop below the anti-frost floor, which the heating holds even
    when "off"; heat loss during the warm-up itself needs no term because the
    learned warm-up rate already includes it.
    """
    if indoor is None:
        return max_minutes
    predicted = indoor
    if gap_hours is not None and gap_hours > 0 and cool_k > 0:
        out_eff = outdoor if outdoor is not None else COOL_FALLBACK_OUTDOOR
        if indoor > out_eff:
            predicted = out_eff + (indoor - out_eff) * math.exp(-cool_k * gap_hours)
            predicted = max(predicted, min(MIN_PREDICT_TEMP, indoor))
    lead = max(rate * (target - predicted), 0.0)
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


def updated_cooling_k(
    k: float, hours_elapsed: float, temp_drop: float, avg_gap: float
) -> float:
    """Fold one observed cool-off into the learned loss constant (EWMA).

    The observation is normalised by the average indoor-outdoor gap over the
    sample — the linear midpoint approximation of the exponential, which is
    indistinguishable from it at the ~1 °C drops the rolling window produces.
    Samples need a real drop, a real duration and a real gap; observations
    and the result are clamped so solar gain, a frozen cloud reading or a
    near-equilibrium mild day cannot poison the estimate.
    """
    if (
        temp_drop < MIN_COOL_SAMPLE_DROP
        or hours_elapsed < MIN_COOL_SAMPLE_HOURS
        or avg_gap < MIN_COOL_SAMPLE_GAP
    ):
        return k
    observed = temp_drop / (hours_elapsed * avg_gap)
    observed = max(MIN_COOL_K, min(MAX_COOL_K, observed))
    new = k + ALPHA * (observed - k)
    return max(MIN_COOL_K, min(MAX_COOL_K, new))
