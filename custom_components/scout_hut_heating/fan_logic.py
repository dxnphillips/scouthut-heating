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
    overheated: bool,
    dt: float | None,
    dt_on: float,
    dt_off: float,
    demand: bool,
    recirc_ok: bool,
    recirc_needs_occupancy: bool = False,
    heating: bool = False,
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
        occupied: someone is in the hall — recent hall motion or a hall event
            actually running (not the pre-heat window: a fan cannot pre-cool a
            room). Gates the summer breeze, and (when recirc_needs_occupancy)
            the no-demand winter recirc path. Active winter heat demand still
            runs the fans regardless of occupancy.
        warm: the head-height comfort estimate (a floor/ceiling blend, the air
            an occupant actually feels) is above the cooling threshold; ``None``
            when the floor temperature is unavailable — with no floor the room's
            warmth is unknown and a breeze is not blown on assumption.
        overheated: that head-height air is at/above the fan-cooling ceiling
            (~35 °C, skin temperature). Above it a breeze blows heat *onto*
            people, so the summer fans are held off (public-health guidance:
            CDC 32 °C for the vulnerable, UK guidance 35 °C).
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
        recirc_needs_occupancy: require hall occupancy for the no-demand recirc
            path. An empty, unheated hut still stratifies from warm fabric, and
            the field cool-off samples measured a fan-mixed overnight decay ≈ the
            still one — so running on that ambient gradient buys no heat retention
            and, with nobody there, no comfort either, at ~150 W. With this set
            the recirc path only runs when someone is actually in the hall;
            active heat demand still runs regardless (the savings case). When
            clear, the legacy demand-independent behaviour stands.
        heating: the hall is in a heating preset (a boost or booking is trying to
            warm it). A forward (down-air) breeze is wind-chill — it would cool
            the very people being heated — so heating forces the winter (reverse,
            up-air destratification) regime even under the summer lockout,
            delivering the made heat down to head height instead of blowing it
            away. Keyed off the *preset*, not instantaneous demand, so the
            direction does not flap as the radiator thermostat cycles.
        currently_winter: the fans are already running in winter mode (so the
            stop thresholds apply instead of the start thresholds).
        run_on_loss: when the ceiling / floor reading is lost, assume
            stratification and keep the winter fans running instead of stopping.
    """
    # The sliders allow dt_off to be set above dt_on, which would invert the
    # hysteresis band (fans stopping the moment they start). Clamp so the stop
    # threshold can never exceed the start threshold.
    dt_off = min(dt_off, dt_on)

    if summer and not heating:
        # Summer cooling: a forward breeze only helps someone who is present, and
        # we need a floor reading to know it is genuinely warm. Without one we do
        # not blow air on assumption (unlike winter). Above the overheat ceiling
        # a fan makes people hotter, not cooler — hold off. Active heating skips
        # this branch entirely (see `heating`): you never blow a cooling draught
        # on a hall that is being warmed — the reverse/destrat branch runs below.
        if warm is None or overheated:
            return False, None, "off"
        if occupied and warm:
            return True, "forward", "summer"
        return False, None, "off"

    # Winter destratification. Gated on real stratification plus "the heat is
    # worth moving" — a heater is actively producing heat (the savings case, run
    # regardless of occupancy), OR residual / leaked ceiling heat is worth
    # harvesting: the floor is below the recirculation cap AND — when
    # recirc_needs_occupancy — someone is actually in the hall to benefit.
    # Harvesting ambient stratification in an empty, unheated hut was measured to
    # buy no heat retention (fan-mixed overnight loss ≈ still loss) while costing
    # ~150 W, so the occupancy gate suppresses that pointless running.
    recirc = recirc_ok and (occupied or not recirc_needs_occupancy)
    worth_moving = demand or recirc
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
