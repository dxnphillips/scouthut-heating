"""Pure destratification / cooling fan decision logic.

Kept free of any Home Assistant imports so it can be unit tested offline and
reasoned about in isolation. The coordinator gathers the live signals (ceiling
and floor temperatures, heat demand, occupancy, the mode switches) and passes
them here; this module only decides whether the fans should run and in which
direction. All timing and safety belong to the Shelly script, never here.

Direction convention (matches the Shelly wiring):
    "reverse" = up air   = winter destratification (O2 closed)
    "forward" = down air = summer cooling          (O2 open)
"""

from __future__ import annotations


def fan_decision(
    *,
    summer: bool,
    occupied: bool,
    warm: bool | None,
    dt: float | None,
    dt_on: float,
    dt_off: float,
    demand: bool,
    currently_winter: bool,
    run_on_loss: bool,
) -> tuple[bool, str | None, str]:
    """Return ``(want_on, direction, mode)``.

    ``direction`` is ``"reverse"`` (winter up air), ``"forward"`` (summer down
    air) or ``None`` when off. ``mode`` is ``"winter"``, ``"summer"`` or
    ``"off"``.

    Fail-safe conditions that force the fans off regardless of season (fans
    disabled, a latched fault) are handled by the caller before this runs.

    Arguments:
        summer: the summer-cooling regime is enabled.
        occupied: the hall is occupied or within a pre-heat window.
        warm: floor temperature is above the cooling threshold; ``None`` when the
            floor temperature is unavailable.
        dt: ceiling minus floor temperature; ``None`` when either reading is
            unavailable or stale.
        dt_on / dt_off: hysteresis band for winter start / stop.
        demand: at least one radiator is actively producing heat.
        currently_winter: the fans are already running in winter mode (so the
            stop thresholds apply instead of the start thresholds).
        run_on_loss: when the ceiling / floor reading is lost, assume
            stratification and keep the winter fans running instead of stopping.
    """
    if summer:
        # Summer cooling: a forward breeze only helps someone who is present, and
        # we need a floor reading to know it is genuinely warm. Without one we do
        # not blow air on assumption (unlike winter).
        if warm is None:
            return False, None, "off"
        if occupied and warm:
            return True, "forward", "summer"
        return False, None, "off"

    # Winter destratification.
    if dt is None:
        # Ceiling / floor lost. Optionally assume stratification and keep running,
        # still gated on heat being produced and the hall occupied.
        if run_on_loss and demand and occupied:
            return True, "reverse", "winter"
        return False, None, "off"

    if currently_winter:
        # Stop on any of: the difference collapsed, heat stopped, hall emptied.
        if dt <= dt_off or not demand or not occupied:
            return False, None, "off"
        return True, "reverse", "winter"

    # Start only when the difference is real, heat is being produced and the hall
    # is occupied (or in a pre-heat window).
    if dt > dt_on and demand and occupied:
        return True, "reverse", "winter"
    return False, None, "off"
