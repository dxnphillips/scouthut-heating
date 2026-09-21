"""Adaptive pre-heat (optimum start) estimation.

Pure functions, free of Home Assistant imports, mirroring fan_logic.py. The
coordinator gathers the live signals (zone temperature, comfort target,
outdoor temperature, the learned warm-up rate) and asks this module how long
the pre-heat lead should be, and how a completed warm-up observation should
update the learned rate.

The model (v1.43.0) is a one-node heat balance rather than the classic linear
"minutes per degree" optimum start. Every hall climb on record draws the same
shape — ``minutes ≈ 31 + 10 × rise`` — a FIXED finish plus a linear bulk: the
Rointes throttle to half power inside the last degree and their oil mass lags,
so the last degree costs 5–10× the first (12–21 min/°C from a 15 °C start,
32–117 from 18.5). A single min/°C rate cannot describe that; it under-leads a
top-up and over-leads a cold start by hours (2026-09-21: a 240-min lead for a
79-min climb). So the lead is built from three things the controller already
holds:

    net gain (°C/h) = radiator gain − fabric leak
                    = 60 / rate − cool_k × (mid-climb indoor − outdoor)
    lead (min)      = 60 × (deficit + APPROACH_TAIL_C) / net gain

``rate`` is the learned GROSS radiator gain (min/°C before leak — the same
entities, learned from real pre-heats that reached target), ``cool_k`` is the
heat-loss constant the cool-off learning already measures (which replaces the
old guessed 1 %/°C cold-weather multiplier with a measured one), and the tail
is the fixed finish. With the finish charged separately, cold-start and
near-target climbs read as ONE family of gross gains, so there is no fast/slow
family left for an out-of-family gate to police — that gate, and the two
single-tick jump guards, are gone; the rise clamp and the sample-selection
rules in the coordinator do their job.
"""

from __future__ import annotations

import math

# Plausibility clamps for the learned radiator gain (minutes per °C, GROSS —
# before the fabric leak is subtracted). The hall's honest cold-climb samples
# read 10-15; the clamps leave room either side without letting one bad sample
# poison the estimate. MAX_RATE is also the seed: at 60 (1 °C/h gross) any leak
# at all drives the net gain to ~zero, so an unlearned zone pins the cap — the
# fail-warm default, exactly as before.
MIN_RATE = 5.0
MAX_RATE = 60.0

# Weight of a new observation in the exponential moving average. 0.3 means the
# estimate settles after a handful of bookings but still tracks the seasons.
ALPHA = 0.3

# Robust EWMA: no single sample may move the rate more than this fraction of
# its value, so one contaminated climb can only nudge it, never yank it.
# Symmetric — this is the one guard that protects BOTH directions, and the only
# one the warm-up learning keeps beyond the cold gate and the sample-selection
# rules. The dangerous corruption is a rate that reads too FAST (short lead,
# cold arrival); a slow sample is fail-safe and folds at full weight.
MAX_RATE_STEP_FRAC = 0.25

# The fixed finish every climb pays, in °C-equivalent at the bulk gain: the
# Rointes throttle to half power (`maintaining`) inside the last degree and
# their oil mass lags the element, so the room's last stretch is not on the
# bulk slope at all. Fitted from the intercept of the honest (rise, minutes)
# pairs on record — 26-31 min at a 10-13 min/°C slope, i.e. 2.0-2.6 °C — and
# set at the top of that range. Charged on EVERY lead (a 1 °C top-up is mostly
# finish: 66 min at gain 15, against the old formula's 42, so no small deficit
# arrives colder than it did) and removed from every sample before its gain is
# read, which is what makes a 4 °C cold climb and a 1 °C top-up read as the
# same gross gain. If near-target arrivals (deficit < 1.5) ever go short with
# the lead well under the cap, this is the constant to raise, not the rate.
APPROACH_TAIL_C = 2.5

# Ignore warm-up samples with less rise than this (°C). Under the tail model a
# 1 °C climb is ~70 % finish, so its implied gain swings ±15 % on a ±0.5 tail
# error and teaches nothing about the slope; only bulk climbs carry the slope,
# and every bulk climb on record is ≥ 3.6 °C. (Was 1.0 under the linear model.)
MIN_SAMPLE_RISE = 2.0

# ...and with less duration than this (minutes): a cloud-lagged reading that
# catches up in one jump would otherwise register an implausibly fast warm-up
# and walk the learned rate to the clamp in a couple of incidents.
MIN_SAMPLE_MINUTES = 10.0

# Never start later than this many minutes before an event, however warm the
# room already is — the calendar look-ahead needs some window to see events.
MIN_LEAD = 15.0

# Cooling prediction never assumes the room drops below the Rointe anti-frost
# floor — the heating system holds it there even when "off".
MIN_PREDICT_TEMP = 7.0

# Booking "hold" (anticipatory maintain — see hold_margin). The response lead
# for a reference-speed hall (min), scaled up for a sluggish one and down for a
# brisk one via the learned gain against this reference. 20 min ~= one-to-two
# of the drive's 15-min staircase steps — enough head-start for the slow loop
# to arrest a fall. The reference was 30 when the rate meant net min/°C (family
# ~40); it is 12 now that the rate means gross gain (family ~12-15) — a UNIT
# conversion that preserves the field response lead (26.7 min → 25 at a gain
# of 15), deliberately NOT coupled to APPROACH_TAIL_C so the hold and the
# pre-heat finish stay separately tunable.
HOLD_LEAD_BASE_MIN = 20.0
HOLD_WARMUP_REF = 12.0

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

# A single reconcile tick may ease the reading down by at most one 0.5 °C
# quantum; a lone tick shedding >= 1.5 °C is a discontinuity, not fabric loss
# — an unmonitored open door/window (the office has no contact to raise the
# opening guard, so its ventilation drops are learned as insulation) or the
# Rointe probe unfreezing and dumping an unknown-duration drop into one
# reading. Either cause makes the sample's per-tick *rate* uninterpretable, so
# it is rejected. 2026-07-22 field evidence: the office EWMA was yanked
# 4.7 -> 24 %/h by two single-tick office steps (24.5 -> 21.5 and 20.5 -> 19.0,
# each in one tick) while a window was open. Set above plausible quantised
# cooling (one 0.5 °C quantum per tick) to protect genuine fast winter
# cool-offs. Fail-safe-neutral: it drops noise, it never clamps a real
# high-loss reading down (which would shorten the lead and risk a cold
# arrival).
MAX_COOL_TICK_DROP = 1.5

# Out-of-family guard (2026-08-11). Once the EWMA has been learned down to a real
# baseline, a sample implying a loss rate a large multiple ABOVE it is not the
# fabric — the fabric cannot suddenly lose 3x faster — it is an unsensored opening
# (a window/door with no contact to raise the opening guard, the office's standing
# problem) or a probe glitch. Reject the whole sample rather than let one event
# corrupt the constant, and let the caller surface it ("a window is probably
# open"). Self-gating: at the coarse 25 seed a 3x multiple (0.75) exceeds MAX_COOL_K
# (0.5) so it can never fire, so this only bites once there IS a trustworthy low
# baseline to be out-of-family against — exactly when it is safe. The ratio is kept
# generous so a genuine winter doubling (wind/infiltration, ~2x) still folds in, and
# only a physically-impossible reading is rejected; the leaky hall, which CAN lose
# fast and has real contact sensors anyway, rarely trips it.
COOL_OUTLIER_RATIO = 3.0

# Robust update: no single sample may move the learned constant more than this
# fraction of its current value. A genuine sustained change (many consistent
# samples) still gets there over a handful of folds, but one anomalous reading that
# slips the outlier test can only nudge the baseline, never yank it. Bounds
# corruption in BOTH directions — including the dangerous one (a spuriously-low
# reading shortening the lead toward a cold arrival), which the outlier ratio,
# being one-sided (high only), does not.
MAX_COOL_STEP_FRAC = 0.25

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
    predicted by Newton cooling toward the outdoor temperature
    (``predicted_room_temp``), and the lead sized for that predicted deficit.
    Decaying across the FULL gap (rather than gap minus the eventual lead)
    slightly over-predicts the cooling — deliberately: the error is a few
    minutes of extra lead in the warm direction, and it keeps the expression
    closed-form. The prediction never assumes a drop below the anti-frost
    floor, which the heating holds even when "off".

    The lead itself is the one-node heat balance (module docstring): the
    radiators' gross gain (``60 / rate`` °C/h) minus the fabric leak over the
    climb (``cool_k`` × the mid-climb indoor-outdoor gap — the same measured
    constant the Newton term uses, so a cold night earns its margin from data
    rather than from a guessed per-degree multiplier), spread over the deficit
    plus the fixed radiator finish (``APPROACH_TAIL_C``). A leak that eats the
    whole gain means the room cannot reach target at this outdoor: the cap,
    which is warm.
    """
    if indoor is None:
        return max_minutes
    predicted = predicted_room_temp(indoor, outdoor, gap_hours, cool_k)
    deficit = target - predicted
    if deficit <= 0:
        return min(MIN_LEAD, max_minutes)
    net = net_gain_c_per_h(rate, predicted, target, outdoor, cool_k)
    if net <= 0:
        return max_minutes
    lead = 60.0 * (deficit + APPROACH_TAIL_C) / net
    return max(min(lead, max_minutes), min(MIN_LEAD, max_minutes))


def predicted_room_temp(
    indoor: float,
    outdoor: float | None,
    gap_hours: float | None,
    cool_k: float,
) -> float:
    """Where the room will be when the pre-heat begins, ``gap_hours`` from now.

    Newton cooling toward outdoor at the learned ``cool_k``; an unreadable
    outdoor assumes the cold fallback (errs warm); never below the anti-frost
    floor the heating holds even when "off". No gap or no learned k → now.
    """
    if gap_hours is None or gap_hours <= 0 or cool_k <= 0:
        return indoor
    out_eff = outdoor if outdoor is not None else COOL_FALLBACK_OUTDOOR
    if indoor <= out_eff:
        return indoor
    predicted = out_eff + (indoor - out_eff) * math.exp(-cool_k * gap_hours)
    return max(predicted, min(MIN_PREDICT_TEMP, indoor))


def net_gain_c_per_h(
    rate: float,
    start: float,
    target: float,
    outdoor: float | None,
    cool_k: float,
) -> float:
    """°C/h the room actually gains during a climb from ``start`` to ``target``.

    Gross radiator gain (``60 / rate``) minus the fabric leak at the mid-climb
    gap (``cool_k × ((start + target)/2 − outdoor)``). The midpoint is the
    linear approximation of the exponential, indistinguishable from it over a
    few degrees — the same simplification the cool-off learning makes. An
    unreadable outdoor assumes the cold fallback (a bigger leak, a longer lead:
    warm). May be ≤ 0, which the caller reads as "unreachable at this leak".
    """
    if rate <= 0:
        return 0.0
    out_eff = outdoor if outdoor is not None else COOL_FALLBACK_OUTDOOR
    gap_mid = max((start + target) / 2.0 - out_eff, 0.0)
    return 60.0 / rate - cool_k * gap_mid


def hold_margin(
    *,
    comfort: float,
    outdoor: float | None,
    cool_k: float,
    warmup_rate: float,
    cap: float,
    lead_base_min: float = HOLD_LEAD_BASE_MIN,
    warmup_ref: float = HOLD_WARMUP_REF,
) -> float:
    """How far ABOVE comfort to hold a room during a booking, to stop it dipping.

    The drive is slow (a 0.5 °C staircase, 15 min/step) and only fires below its
    target, so on a falling evening it catches the room reactively at comfort and
    then lags — the room undershoots while the radiators spin up. During a booking
    we KNOW comfort is wanted for the whole slot, so we pre-empt the dip by holding
    the drive's target a little above comfort. The margin is sized from BOTH learned
    rates, so it is exactly the head-start the fall warrants and no more:

      * how fast the room is losing heat — Newton cooling at comfort,
        ``cool_k · (comfort − outdoor)`` °C/h (the same gap-normalised heat-loss
        the pre-heat uses), so a cold night earns a bigger margin, a mild one ~none;
      * how sluggish the radiators are to respond — the learned warm-up rate scales
        the response lead (``lead_base_min · warmup_rate / warmup_ref``), so a
        slow-to-heat hall gets more lead than a brisk one.

    margin = cooling_rate × response_lead. Model-based (comfort + outdoor + the two
    learned constants), NOT the live indoor trend, so applying the heat cannot
    collapse the margin and set up an oscillation — it is a stable function of the
    conditions. Zero when the outdoor is at/above comfort (nothing to hold against),
    when either rate is unlearned/zero, or when the cap is 0 (the off switch).
    Clamped to ``cap``. Unreadable outdoor assumes the cold fallback (errs warm).
    """
    if cap <= 0 or cool_k <= 0 or warmup_rate <= 0:
        return 0.0
    out = outdoor if outdoor is not None else COOL_FALLBACK_OUTDOOR
    gap = comfort - out
    if gap <= 0:
        return 0.0
    cooling_rate_per_h = cool_k * gap
    lead_min = lead_base_min * (warmup_rate / warmup_ref)
    margin = cooling_rate_per_h * (lead_min / 60.0)
    return max(0.0, min(margin, cap))


def updated_rate(
    rate: float,
    minutes_elapsed: float,
    temp_rise: float,
    avg_gap: float = 0.0,
    cool_k: float = 0.0,
) -> float:
    """Fold one observed warm-up into the learned gross gain (EWMA).

    The observation is the lead formula run backwards (``warmup_observed_rate``):
    the finish is added back to the rise and the leak at the sample's average
    gap added back to the net gain, so a 4 °C cold climb and a 2 °C top-up on a
    cold night both read as the radiators' gross gain and fold into ONE family.
    Samples with too little rise or duration are ignored; observations and the
    result are clamped into the plausible band; and the robust step cap bounds
    how far any one sample can move the rate in either direction — the one
    guard kept, because it protects the dangerous (fast) direction without a
    baseline to be out-of-family against. Which samples reach here at all is the
    coordinator's job (a real pre-heat that reached target, in cold conditions).
    """
    observed = warmup_observed_rate(minutes_elapsed, temp_rise, avg_gap, cool_k)
    if observed is None:
        return rate
    new = rate + ALPHA * (observed - rate)
    # Robust: cap how far one sample can move the rate, either direction.
    new = max(rate * (1 - MAX_RATE_STEP_FRAC), min(rate * (1 + MAX_RATE_STEP_FRAC), new))
    return max(MIN_RATE, min(MAX_RATE, new))


def warmup_observed_rate(
    minutes_elapsed: float,
    temp_rise: float,
    avg_gap: float = 0.0,
    cool_k: float = 0.0,
) -> float | None:
    """The gross radiator gain (min/°C before leak) one warm-up implies, clamped,
    or None if the sample is too small to use.

    ``(temp_rise + APPROACH_TAIL_C)`` over the hours is the net gain the room
    showed; adding the leak the fabric took over the same hours
    (``cool_k × avg_gap``) gives what the radiators actually delivered. Exactly
    what ``required_lead_minutes`` assumes, so a lead sized from a folded rate
    reproduces the climb it was learned from.
    """
    if temp_rise < MIN_SAMPLE_RISE or minutes_elapsed < MIN_SAMPLE_MINUTES:
        return None
    hours = minutes_elapsed / 60.0
    gross = (temp_rise + APPROACH_TAIL_C) / hours + cool_k * max(avg_gap, 0.0)
    if gross <= 0:
        return None
    return max(MIN_RATE, min(MAX_RATE, 60.0 / gross))


def updated_cooling_k(
    k: float,
    hours_elapsed: float,
    temp_drop: float,
    avg_gap: float,
    max_tick_drop: float = 0.0,
) -> float:
    """Fold one observed cool-off into the learned loss constant (EWMA).

    The observation is normalised by the average indoor-outdoor gap over the
    sample — the linear midpoint approximation of the exponential, which is
    indistinguishable from it at the ~1 °C drops the rolling window produces.
    Samples need a real drop, a real duration and a real gap; observations
    and the result are clamped so solar gain, a frozen cloud reading or a
    near-equilibrium mild day cannot poison the estimate. ``max_tick_drop``
    (the largest single-tick fall seen over the sample) rejects the whole
    sample when a discontinuity — an unmonitored opening or a probe
    unfreezing — dumped an uninterpretable step into it.
    """
    if (
        temp_drop < MIN_COOL_SAMPLE_DROP
        or hours_elapsed < MIN_COOL_SAMPLE_HOURS
        or avg_gap < MIN_COOL_SAMPLE_GAP
        or max_tick_drop >= MAX_COOL_TICK_DROP
    ):
        return k
    observed = temp_drop / (hours_elapsed * avg_gap)
    observed = max(MIN_COOL_K, min(MAX_COOL_K, observed))
    # Out-of-family: implausibly high vs the established baseline -> an opening or
    # glitch, not the fabric. Reject the whole sample (leave k untouched).
    if cooling_sample_is_outlier(k, observed):
        return k
    new = k + ALPHA * (observed - k)
    # Robust: cap how far one fold can move the baseline, so no single reading can
    # yank it (only a sustained run of consistent samples moves it far).
    new = max(k * (1 - MAX_COOL_STEP_FRAC), min(k * (1 + MAX_COOL_STEP_FRAC), new))
    return max(MIN_COOL_K, min(MAX_COOL_K, new))


def cooling_observed_k(hours_elapsed: float, temp_drop: float, avg_gap: float) -> float | None:
    """The raw observed loss fraction for a cool-off, or None if unusable.

    Kept separate so the coordinator can judge the same value the EWMA fold sees
    (for the out-of-family notification) without duplicating the arithmetic.
    """
    if hours_elapsed <= 0 or avg_gap <= 0:
        return None
    return max(MIN_COOL_K, min(MAX_COOL_K, temp_drop / (hours_elapsed * avg_gap)))


def cooling_sample_is_outlier(k: float, observed: float) -> bool:
    """Whether an observed loss fraction is implausibly far above the baseline k.

    True means "the fabric cannot lose this fast — something (an unsensored
    opening, a glitch) changed": reject the sample and surface it. Only meaningful
    once k is a real learned baseline; at the coarse seed the MAX_COOL_K clamp
    keeps any observed below the ratio, so it cannot fire there.
    """
    return k > 0 and observed > COOL_OUTLIER_RATIO * k
