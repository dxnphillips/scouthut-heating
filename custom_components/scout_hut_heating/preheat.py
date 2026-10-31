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
    lead (min)      = 60 × (deficit + tail) / net gain

``rate`` is the learned GROSS radiator gain (min/°C before leak — the same
entities, learned from real pre-heats), ``cool_k`` is the heat-loss constant
the cool-off learning already measures (which replaces the old guessed 1 %/°C
cold-weather multiplier with a measured one), and ``tail`` is the start-up
allowance: the degrees' worth of time every climb loses to the oil mass
warming up and the Rointes throttling near target. With it charged
separately, cold-start and near-target climbs read as ONE family of gross
gains, so there is no fast/slow family left for an out-of-family gate to
police — that gate, and the two single-tick jump guards, are gone; the rise
clamp and the sample-selection rules in the coordinator do their job.

The tail was a fixed 2.5 °C until v1.51.0. October 2026 showed it too big: the
lead came out 4–23 min early on shallow climbs and 24–38 min LATE on the two
cold-soaked ones (2026-10-21 from 11.9, 10-28 from 13.4), because a big tail
with a fast gain over-charges a short climb and under-charges a long one. A
straight line through the nine October hall climbs, ``rise + leak = gain ×
hours − tail``, puts the tail near 0.2–0.5 and the gross gain near 17–19
min/°C. So the tail is now LEARNED per zone, together with the gain, from the
same line (``learned_warmup``), seeded at 1.0.
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

# The start-up allowance ("tail"), in °C-equivalent at the bulk gain: the time
# every climb loses before and after the bulk slope (the oil mass warming up,
# the Rointes throttling to half power inside the last degree). Charged on
# EVERY lead and removed from every sample before its gain is read, which is
# what makes a 4 °C cold climb and a 1 °C top-up read as the same gross gain.
# Learned per zone since v1.51.0 (``learned_warmup``); this is the seed, and
# the bounds and per-sample step keep one odd climb from yanking it.
TAIL_SEED_C = 1.0
MIN_TAIL_C = 0.5
MAX_TAIL_C = 3.0
MAX_TAIL_STEP_C = 0.5

# The tail and the gain are read off a straight line through the zone's recent
# climbs, which needs enough of them and enough spread in their lengths to tell
# the intercept from the slope. Until then only the gain is learned (the EWMA,
# with the tail held where it is).
FIT_MIN_SAMPLES = 4
FIT_MIN_SPREAD_MIN = 30.0
WARMUP_HISTORY_MAX = 10

# The one-time conversion of a gain learned under the fixed 2.5 tail onto the
# 1.0 seed: (3 + 2.5) / (3 + 1), the factor that keeps a 3 °C climb's lead
# unchanged, so shorter climbs start later and longer ones earlier.
TAIL_CONVERSION = (3.0 + 2.5) / (3.0 + TAIL_SEED_C)

# Ignore warm-up samples with less rise than this (°C): a 1 °C climb is mostly
# tail, so its implied gain swings on a small tail error and teaches little
# about the slope. (Was 1.0 under the linear model.)
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
# ~40); it was 12 when the gross gain carried the fixed 2.5 tail (family
# ~12-15), and 16.5 since v1.51.0 converted the gains by TAIL_CONVERSION onto
# the learned tail — UNIT conversions both, preserving the field response lead,
# deliberately NOT coupled to the tail so the hold and the pre-heat stay
# separately tunable.
HOLD_LEAD_BASE_MIN = 20.0
HOLD_WARMUP_REF = 16.5

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
    tail: float = TAIL_SEED_C,
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
    plus the learned start-up allowance (``tail``). A leak that eats the
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
    lead = 60.0 * (deficit + tail) / net
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
    tail: float = TAIL_SEED_C,
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
    observed = warmup_observed_rate(minutes_elapsed, temp_rise, avg_gap, cool_k, tail)
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
    tail: float = TAIL_SEED_C,
) -> float | None:
    """The gross radiator gain (min/°C before leak) one warm-up implies, clamped,
    or None if the sample is too small to use.

    ``(temp_rise + tail)`` over the hours is the net gain the room
    showed; adding the leak the fabric took over the same hours
    (``cool_k × avg_gap``) gives what the radiators actually delivered. Exactly
    what ``required_lead_minutes`` assumes, so a lead sized from a folded rate
    reproduces the climb it was learned from.
    """
    if temp_rise < MIN_SAMPLE_RISE or minutes_elapsed < MIN_SAMPLE_MINUTES:
        return None
    hours = minutes_elapsed / 60.0
    gross = (temp_rise + tail) / hours + cool_k * max(avg_gap, 0.0)
    if gross <= 0:
        return None
    return max(MIN_RATE, min(MAX_RATE, 60.0 / gross))


def warmup_sample_usable(minutes_elapsed: float, temp_rise: float) -> bool:
    """Big enough to learn from (the same floor the gain has always used)."""
    return temp_rise >= MIN_SAMPLE_RISE and minutes_elapsed >= MIN_SAMPLE_MINUTES


def fitted_tail(history: list[tuple[float, float, float, float]]) -> float | None:
    """The start-up allowance a zone's recent climbs imply, or None.

    Each climb is ``(minutes, rise, avg_gap, cool_k)``. Under the lead model the
    room rises at the net gain after losing ``tail`` degrees' worth of time, so
    ``rise + cool_k × avg_gap × hours = gain × hours − tail`` — a straight line
    in the hours whose intercept is minus the tail. Fitted by least squares
    once there are ``FIT_MIN_SAMPLES`` climbs whose lengths spread at least
    ``FIT_MIN_SPREAD_MIN`` (a slope and an intercept cannot be told apart from
    climbs that all took the same time). Unclamped: ``learned_warmup`` bounds it.
    """
    if len(history) < FIT_MIN_SAMPLES:
        return None
    xs = [m / 60.0 for m, _, _, _ in history]
    if (max(xs) - min(xs)) * 60.0 < FIT_MIN_SPREAD_MIN:
        return None
    ys = [r + k * max(g, 0.0) * x for (_, r, g, k), x in zip(history, xs)]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return None
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    if slope <= 0:
        return None
    return -(my - slope * mx)


def learned_warmup(
    rate: float, tail: float, history: list[tuple[float, float, float, float]]
) -> tuple[float, float, str]:
    """Fold the newest climb (the last in ``history``) into the zone's gain and
    start-up allowance: ``(new_rate, new_tail, method)``.

    ``"fit"`` once the history can carry a straight line (``fitted_tail``):
    the tail moves toward the fitted intercept by at most ``MAX_TAIL_STEP_C``,
    bounded to ``[MIN_TAIL_C, MAX_TAIL_C]``, and the gain is then re-fitted
    with that tail held (least squares through the same climbs), so the pair
    the lead uses is always consistent; the gain moves at most
    ``MAX_RATE_STEP_FRAC`` per climb. ``"ewma"`` before that: the tail stays
    and the newest climb folds into the gain as it always has.
    """
    minutes, rise, avg_gap, cool_k = history[-1]
    fit = fitted_tail(history)
    if fit is None:
        return updated_rate(rate, minutes, rise, avg_gap, cool_k, tail), tail, "ewma"
    new_tail = max(tail - MAX_TAIL_STEP_C, min(tail + MAX_TAIL_STEP_C, fit))
    new_tail = max(MIN_TAIL_C, min(MAX_TAIL_C, new_tail))
    xs = [m / 60.0 for m, _, _, _ in history]
    ys = [r + k * max(g, 0.0) * x for (_, r, g, k), x in zip(history, xs)]
    gain = sum(x * (y + new_tail) for x, y in zip(xs, ys)) / sum(x * x for x in xs)
    if gain <= 0:
        return updated_rate(rate, minutes, rise, avg_gap, cool_k, tail), tail, "ewma"
    observed = max(MIN_RATE, min(MAX_RATE, 60.0 / gain))
    new = max(rate * (1 - MAX_RATE_STEP_FRAC), min(rate * (1 + MAX_RATE_STEP_FRAC), observed))
    return max(MIN_RATE, min(MAX_RATE, new)), new_tail, "fit"


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
