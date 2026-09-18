"""Land ON target, not past it: the coast-aware final approach (v1.39.0).

A Rointe's oil mass keeps releasing after its element cuts out, so driving the
elements all the way to target guarantees the room lands above it (field
2026-09-17: `peak_over` 1.62 °C for 111 of a 150-min booking, the fourth mild
instance in nine days). On the last fraction of the climb — and only while the
panels are measurably hot — the pushed setpoint is eased BELOW target so the
stored heat carries the room in.

Every safety property here is a gate, not a guess, and each has a test: hot
panels required, room average already within the allowance, time-boxed once per
approach, instant restoration of the full committed drive when it lets go.
"""

from datetime import timedelta

import pytest

from custom_components.scout_hut_heating.coordinator import (
    DRIVE_COAST_ALLOWANCE,
    DRIVE_COAST_MAX_MINUTES,
)
from custom_components.scout_hut_heating.drive import STEP, update_drive
from scout_testkit import E, ZA, make_controller, set_registry

HB, HF = "climate.hall_back", "climate.hall_front"
HB_SURFACE = "sensor.hall_back_surface_temperature"


# --- The pure controller -----------------------------------------------------
def _drive(**kw):
    args = dict(
        target=19.0, probe=19.0, outdoor=None, heatloss_frac=0.0, cap=24.0,
        prev_stair=0.0, minutes_since_step=0.0,
    )
    args.update(kw)
    return update_drive(**args)


def test_without_a_coast_the_drive_never_pushes_below_target():
    pushed, *_ = _drive(probe=18.0)
    assert pushed >= 19.0


def test_a_coast_eases_the_pushed_setpoint_below_target():
    pushed, *_ = _drive(probe=18.75, coast=0.5)
    assert pushed == 18.5


def test_the_coast_leaves_the_staircase_intact_for_instant_recovery():
    # The committed overdrive is NOT unwound while easing, so withdrawing the
    # coast restores the full drive on the very next tick rather than over a
    # 15-min-per-step climb back.
    eased, stair, *_ = _drive(probe=18.75, prev_stair=1.0, coast=0.5)
    assert eased == 18.5
    assert stair == 1.0
    restored, *_ = _drive(probe=18.75, prev_stair=stair, coast=0.0)
    assert restored == 20.0


def test_easing_does_not_escalate_an_existing_overdrive():
    # Same withhold-only rule as the approach hold: while the mass is landing the
    # room, a probe still reading short must not wind the staircase up further.
    _pushed, stair, evaluated, _frozen, approach_held = _drive(
        probe=18.0, prev_stair=1.0, minutes_since_step=20.0, coast=0.5
    )
    assert evaluated is True
    assert approach_held is True
    assert stair == 1.0  # held, not escalated


def test_easing_never_blocks_the_initial_climb():
    # prev_stair <= 0 is the sacred first climb: it steps even while a coast is
    # notionally active, so a cold room can never be starved by this.
    _pushed, stair, *_ = _drive(
        probe=17.0, prev_stair=0.0, minutes_since_step=20.0, coast=0.5
    )
    assert stair == STEP


# --- The coordinator's gates -------------------------------------------------
def _ctrl(panel=None, comfort_temp=19.0):
    ctrl, hass = make_controller()
    ctrl._numbers["hall_comfort_temp"].native_value = comfort_temp
    if panel is not None:
        set_registry(
            entries_by_device={"dev_hall_back": [HB_SURFACE]},
            entity_devices={HB: "dev_hall_back"},
        )
        hass.states.set(HB_SURFACE, str(panel))
    return ctrl, hass


def _coast(ctrl, zone_avg, comfort=True, at=None):
    return ctrl._drive_coast(ZA, 19.0, zone_avg, comfort, at or ctrl._now())


def _events(ctrl, name):
    return [e for e in ctrl.audit.to_list() if e.get("event") == name]


def test_hot_panels_and_a_room_on_the_approach_engage_the_coast():
    ctrl, _ = _ctrl(panel=55.0)
    assert _coast(ctrl, 18.75) == DRIVE_COAST_ALLOWANCE
    (evt,) = _events(ctrl, "drive_coast_ease")
    assert evt["zone"] == ZA
    assert evt["zone_avg"] == 18.75


def test_cold_panels_never_engage_it():
    # No stored heat to deliver — there is nothing for the mass to land, so the
    # drive must keep firing normally.
    ctrl, _ = _ctrl(panel=19.0)
    assert _coast(ctrl, 18.75) == 0.0
    assert _events(ctrl, "drive_coast_ease") == []


def test_an_install_with_no_surface_sensor_never_engages_it():
    ctrl, _ = _ctrl(panel=None)  # nothing mapped -> no evidence of stored heat
    assert _coast(ctrl, 18.75) == 0.0


def test_a_genuine_cold_climb_is_untouched():
    # The whole cold-arrival guard: far below target, the easing cannot engage
    # however hot the panels are.
    ctrl, _ = _ctrl(panel=60.0)
    assert _coast(ctrl, 15.0) == 0.0


def test_a_zone_not_being_driven_to_comfort_is_untouched():
    ctrl, _ = _ctrl(panel=55.0)
    assert _coast(ctrl, 18.75, comfort=False) == 0.0


def test_falling_out_of_the_band_releases_the_drive_at_once():
    ctrl, _ = _ctrl(panel=55.0)
    assert _coast(ctrl, 18.75) == DRIVE_COAST_ALLOWANCE
    # The tail did not materialise and the room slipped: hand the drive straight
    # back, no waiting.
    assert _coast(ctrl, 18.0) == 0.0
    assert ZA not in ctrl._drive_coast_since


def test_the_easing_is_time_boxed_and_does_not_re_arm_in_the_same_approach():
    ctrl, _ = _ctrl(panel=55.0)
    now = ctrl._now()
    assert _coast(ctrl, 18.75, at=now) == DRIVE_COAST_ALLOWANCE
    later = now + timedelta(minutes=DRIVE_COAST_MAX_MINUTES + 1)
    assert _coast(ctrl, 18.75, at=later) == 0.0  # window used up
    # Still in the band with hot panels, so it must NOT start another window.
    assert _coast(ctrl, 18.9, at=later + timedelta(minutes=1)) == 0.0


def test_a_fresh_approach_gets_a_new_window():
    ctrl, _ = _ctrl(panel=55.0)
    now = ctrl._now()
    _coast(ctrl, 18.75, at=now)
    expired = now + timedelta(minutes=DRIVE_COAST_MAX_MINUTES + 1)
    assert _coast(ctrl, 18.75, at=expired) == 0.0
    # The room leaves the band (a new climb begins), which re-arms it.
    assert _coast(ctrl, 17.0, at=expired + timedelta(minutes=1)) == 0.0
    assert _coast(ctrl, 18.75, at=expired + timedelta(minutes=2)) == DRIVE_COAST_ALLOWANCE


def test_the_episode_records_what_the_mass_actually_delivered():
    # The measurement that should set DRIVE_COAST_ALLOWANCE: the allowance is a
    # conservative seed, this is the evidence to replace it with.
    ctrl, _ = _ctrl(panel=55.0)
    now = ctrl._now()
    _coast(ctrl, 18.5, at=now)  # engage
    _coast(ctrl, 19.1, at=now + timedelta(minutes=5))  # mass carries it up
    _coast(ctrl, 19.0, at=now + timedelta(minutes=8))  # settles back
    _coast(ctrl, 17.5, at=now + timedelta(minutes=10))  # out of band -> episode ends
    (evt,) = _events(ctrl, "drive_coast_end")
    assert evt["start"] == 18.5
    assert evt["peak"] == pytest.approx(19.1)
    assert evt["rise"] == pytest.approx(0.6)
    assert evt["minutes"] == pytest.approx(10.0)


def test_the_closing_reading_counts_toward_the_rise():
    # On this plant the tail is WHAT ENDS the episode: the mass carries the room
    # past target, the room reads warm, the zone drops out of comfort — and the
    # gate fails on that very tick. Reading the peak only from the last still-easing
    # tick threw the measurement away exactly when there was one to make (field
    # 2026-09-17: peak 19.38 / rise 0.0 logged while `ended` was 20.25 — the mass
    # had delivered +0.87, nearly twice the allowance).
    ctrl, _ = _ctrl(panel=55.0)
    now = ctrl._now()
    _coast(ctrl, 19.38, at=now)
    # The elements have cut, the room sails up on stored heat and goes warm, so the
    # zone leaves comfort — the episode ends on the tick that carries the evidence.
    ctrl._drive_coast(ZA, 19.5, 20.25, False, now + timedelta(minutes=3))
    (evt,) = _events(ctrl, "drive_coast_end")
    assert evt["ended"] == pytest.approx(20.25)
    assert evt["peak"] == pytest.approx(20.25)
    assert evt["rise"] == pytest.approx(0.87)


# --- End to end through the reconciler ---------------------------------------
def _wire_hall(panel):
    """Both hall heaters with a comfort number; hall_back also has a panel probe."""
    set_registry(
        entries_by_device={
            "dhb": [HB, "number.hall_back_comfort_temperature", HB_SURFACE],
            "dhf": [HF, "number.hall_front_comfort_temperature"],
        },
        entity_devices={HB: "dhb", HF: "dhf"},
    )
    return panel


def _last_setpoint(hass, climate):
    val = None
    for c in hass.services.calls:
        if c["domain"] == "climate" and c["service"] == "set_temperature":
            ids = c["data"].get("entity_id")
            ids = ids if isinstance(ids, list) else [ids]
            if climate in ids:
                val = c["data"].get("temperature")
    return val


def test_the_shared_zone_is_a_driven_zone_too():
    # `_reconcile_drive` loops all THREE driven zones, but `_zone_panels_hot`
    # resolved climates through ZONE_CLIMATES (hall + office only), so the shared
    # pass raised KeyError('shared') every 30 s. Field 2026-09-17: that ran for
    # 5 h, abandoning the fans (stuck blowing forward through two heated bookings)
    # and the trace along with it.
    from scout_testkit import E

    kitchen = E["shared"][0]
    surface = "sensor.kitchen_surface_temperature"
    set_registry(
        entries_by_device={"dev_kitchen": [surface]},
        entity_devices={kitchen: "dev_kitchen"},
    )
    ctrl, hass = make_controller()
    hass.states.set(surface, "55.0")
    assert ctrl._zone_panels_hot("shared", 18.75) is True
    assert ctrl._drive_coast("shared", 19.0, 18.75, True, ctrl._now()) == DRIVE_COAST_ALLOWANCE


def test_a_full_reconcile_drives_every_zone_without_a_failing_step():
    from scout_testkit import booking, motion, run, shared_temp

    ctrl, _hass = make_controller()
    booking(ctrl, ZA)
    motion(ctrl, "hall")
    motion(ctrl, "kitchen")
    # Just below target: cold enough to be DRIVEN to comfort, close enough that the
    # coast gate reaches the panel check rather than short-circuiting before it.
    shared_temp(ctrl, 19.25)  # shared_comfort_temp default is 19.5
    run(ctrl.async_reconcile())
    assert ctrl.applied["shared"] == "comfort"
    # v1.40.0 isolates a throwing step, so "did not raise" proves nothing — the
    # failing set is what tells us the shared drive pass completed.
    assert ctrl.diagnostics_data()["state"]["reconcile_failing"] == []


def test_the_reconciler_lands_a_below_target_setpoint_on_the_hall():
    from scout_testkit import booking, motion, run

    _wire_hall(55.0)
    ctrl, hass = make_controller()
    ctrl._numbers["hall_comfort_temp"].native_value = 19.0
    ctrl._numbers["booking_hold_cap"].native_value = 0  # isolate the drive mechanics
    booking(ctrl, ZA)
    motion(ctrl, "hall")
    hass.states.set(E["weather"], "cloudy", {"temperature": 12.0})
    # Room on the last fraction of the climb, panels hot from the burn.
    for climate in (HB, HF):
        hass.states.set(climate, "heat", {"current_temperature": 18.75})
    hass.states.set(HB_SURFACE, "55.0")

    run(ctrl.async_reconcile())
    # Elements cut early: the live setpoint is BELOW the 19.0 comfort target so the
    # stored panel heat lands the last fraction instead of pushing past it.
    assert _last_setpoint(hass, HB) == 19.0 - DRIVE_COAST_ALLOWANCE
    assert ctrl.diagnostics_data()["state"]["drive"]["coast_eased"] == [ZA]

    # Panels cool: the drive takes over again immediately, at or above target.
    hass.states.set(HB_SURFACE, "19.0")
    run(ctrl.async_reconcile())
    assert _last_setpoint(hass, HB) >= 19.0

