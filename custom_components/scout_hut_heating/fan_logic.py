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
    recirc_ok: bool,
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
        occupied: the hall is occupied or within a pre-heat window. Only gates the
            summer breeze; winter destratification runs regardless of occupancy so
            it can knock down the hot ceiling layer and cut roof heat-loss even
            when people are only in the office.
        warm: floor temperature is above the cooling threshold; ``None`` when the
            floor temperature is unavailable.
        dt: ceiling minus floor temperature; ``None`` when either reading is
            unavailable or stale.
        dt_on / dt_off: hysteresis band for winter start / stop.
        demand: at least one radiator (any zone) is actively producing heat.
        recirc_ok: the floor is below the recirculation cap, so ceiling heat is
            still worth bringing down even with no active demand (harvests residual
            heat after a heater cuts out, and heat leaking in from other zones).
            This is how real destratification controllers run — on the ceiling-floor
            difference, decoupled from the heater's on/off cycle — backing off only
            once the occupied zone is genuinely warm.
        currently_winter: the fans are already running in winter mode (so the
            stop thresholds apply instead of the start thresholds).
        run_on_loss: when the ceiling / floor reading is lost, assume
            stratification and keep the winter fans running instead of stopping.
    """
    # The sliders allow dt_off to be set above dt_on, which would invert the
    # hysteresis band (fans stopping the moment they start). Clamp so the stop
    # threshold can never exceed the start threshold.
    dt_off = min(dt_off, dt_on)

    if summer:
        # Summer cooling: a forward breeze only helps someone who is present, and
        # we need a floor reading to know it is genuinely warm. Without one we do
        # not blow air on assumption (unlike winter).
        if warm is None:
            return False, None, "off"
        if occupied and warm:
            return True, "forward", "summer"
        return False, None, "off"

    # Winter destratification. Runs for loss reduction as well as comfort, so it is
    # gated on real stratification plus "the heat is worth moving" — i.e. a heater
    # is actively producing heat, OR the occupied zone is still below the
    # recirculation cap (so residual / leaked ceiling heat is worth harvesting). Not
    # gated on hall occupancy.
    worth_moving = demand or recirc_ok
    if dt is None:
        # Ceiling / floor lost. Optionally assume stratification and keep running,
        # still gated on heat being produced.
        if run_on_loss and demand:
            return True, "reverse", "winter"
        return False, None, "off"

    if currently_winter:
        # Stop on either: the difference collapsed, or the heat is no longer worth
        # moving (heater off and the room already warm enough).
        if dt <= dt_off or not worth_moving:
            return False, None, "off"
        return True, "reverse", "winter"

    # Start when the difference is real and the heat is worth moving.
    if dt > dt_on and worth_moving:
        return True, "reverse", "winter"
    return False, None, "off"
