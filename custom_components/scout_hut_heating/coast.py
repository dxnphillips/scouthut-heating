"""Passive-arrival ("will it get there on its own?") prediction.

Pure functions, free of Home Assistant imports, mirroring ``preheat.py`` and
``fan_logic.py``. This is the inverse of ``preheat.required_lead_minutes``:
instead of "how long to heat the room up", it asks "at the room's *measured*
current rise rate, will it reach the comfort band on its own before it is
needed?" — so the controller can decline to spend electric heat the building is
about to supply for free (solar onto the big uninsulated roof, occupancy load,
warm-fabric release).

This is deliberately the most conservative piece in the system, because it is
the inverse of a comfort guarantee: a wrong "it'll coast" means a cold arrival,
which is worse than a little wasted heat. So it is **comfort-lean by
construction** — it answers "yes, coast" only when the room is *genuinely,
measurably* warming AND will arrive with both a temperature deadband and a time
margin to spare. Every uncertain input (no reading, no measured rate, not
warming, not enough spare time) answers "no" → heat.

Crucially the rise rate it is fed must be measured while the heaters are IDLE
(no heat demand) — see ``ScoutController._passive_rise_rate``. A rate measured
while the radiators are driving is the heater's work, not free gain;
extrapolating it to cut the heat would be exactly the feedback trap this must
avoid. Measuring only idle-room rise makes the signal honest (it is free gain
or nothing) and non-oscillating (the moment heat is applied the measurement
invalidates, so it cannot chatter the heat on and off).
"""

from __future__ import annotations

# A room within this many °C of target counts as "arrived": the Rointe room
# readings are coarse (0.5 °C quanta, cloud-lagged) and holding the last
# fraction of a degree is not worth a pre-heat.
DEADBAND = 0.3

# Below this measured rise (°C/min ≈ 0.5 °C/h) the room is not meaningfully
# warming — treat it as static and heat. Filters sensor j/noise from a genuine
# solar/occupancy climb.
MIN_RISE_RATE = 0.008

# Require the passive climb to reach the band with this fraction of the gap
# still to spare. The margin is the buffer that makes resuming heat safe if the
# free gain fades (a cloud crosses): even a late switch back to the radiators
# still has slack before the deadline. 0.30 = arrive with ~a third of the lead
# unused.
TIME_MARGIN_FRAC = 0.30


def will_coast_to_target(
    *,
    indoor: float | None,
    target: float,
    rise_rate: float | None,
    gap_min: float | None,
    deadband: float = DEADBAND,
    min_rise_rate: float = MIN_RISE_RATE,
    time_margin_frac: float = TIME_MARGIN_FRAC,
) -> bool:
    """True when the room will passively reach the comfort band by the deadline.

    ``indoor`` current room temperature (the coldest end, matching how the
    pre-heat sizes its deficit); ``target`` the comfort setpoint; ``rise_rate``
    the measured idle-room warming in °C/min (None if unmeasurable / heaters
    were driving); ``gap_min`` minutes until the room is needed.

    Comfort-lean: returns False (→ heat) unless the room is *measurably*
    warming (``rise_rate >= min_rise_rate``) AND either is already inside the
    band, or will reach ``target - deadband`` at the current rate with a time
    margin to spare. Any missing input returns False.
    """
    if indoor is None or rise_rate is None or gap_min is None:
        return False
    # Require a genuine, measured climb for ANY coast decision — even a room
    # already at the band must be *seen* rising (not merely static or
    # drifting down) before heat is withheld. This is the whole comfort-lean
    # stance: never decline heat on an assumption, only on an observation.
    if rise_rate < min_rise_rate:
        return False
    band_target = target - deadband
    if indoor >= band_target:
        return True
    needed_min = (band_target - indoor) / rise_rate
    return needed_min * (1 + time_margin_frac) <= gap_min
