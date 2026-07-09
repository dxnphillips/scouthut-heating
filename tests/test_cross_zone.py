"""Links and interactions between the three zones (reconcile-level)."""

from scout_testkit import (
    PRESET_COMFORT,
    PRESET_ECO,
    PRESET_ICE,
    ZA,
    ZB,
    booking,
    boost,
    make_controller,
    motion,
    on,
    run,
    E,
)


def test_boost_a_drives_shared_to_comfort():
    ctrl, _ = make_controller()
    boost(ctrl, ZA)
    run(ctrl.async_reconcile())
    assert ctrl.applied["shared"] == PRESET_COMFORT


def test_boost_a_does_not_turn_on_water():
    ctrl, _ = make_controller()
    boost(ctrl, ZA)
    run(ctrl.async_reconcile())
    assert ctrl.water_on is False


def test_both_alarms_ice_shared_and_off_water():
    ctrl, hass = make_controller()
    on(hass, E["alarm_main"])
    on(hass, E["alarm_office"])
    run(ctrl.async_reconcile())
    assert ctrl.applied["shared"] == PRESET_ICE
    assert ctrl.water_on is False


def test_internal_door_plus_hall_exterior_ices_both_zones():
    ctrl, hass = make_controller()
    on(hass, E["internal"])
    on(hass, E["a_door"])  # a hall exterior opening
    run(ctrl.async_reconcile())
    assert ctrl.opening_ice[ZA] is True
    assert ctrl.opening_ice[ZB] is True
    assert ctrl.applied[ZA] == PRESET_ICE
    assert ctrl.applied[ZB] == PRESET_ICE


def test_internal_door_plus_office_exterior_ices_both_zones():
    ctrl, hass = make_controller()
    on(hass, E["internal"])
    on(hass, E["b_window"])  # an office exterior opening
    run(ctrl.async_reconcile())
    assert ctrl.applied[ZA] == PRESET_ICE
    assert ctrl.applied[ZB] == PRESET_ICE


def test_internal_door_alone_does_not_ice():
    ctrl, hass = make_controller()
    on(hass, E["internal"])  # no exterior opening -> harmless shared air
    run(ctrl.async_reconcile())
    assert ctrl.opening_ice[ZA] is False
    assert ctrl.opening_ice[ZB] is False


def test_hall_booking_drives_shared_eco():
    ctrl, _ = make_controller()
    booking(ctrl, ZA)
    run(ctrl.async_reconcile())
    assert ctrl.applied["shared"] == PRESET_ECO


def test_kitchen_motion_multi_zone_effect():
    # Kitchen motion: shared -> eco, water -> on, but the empty heated zones
    # are left alone (someone is in the building, just not the hall/office).
    ctrl, _ = make_controller()
    motion(ctrl, "kitchen")
    run(ctrl.async_reconcile())
    assert ctrl.applied["shared"] == PRESET_ECO
    assert ctrl.water_on is True
    assert ctrl.applied[ZA] is None
    assert ctrl.applied[ZB] is None


def test_partial_alarm_keeps_other_zone_heating():
    # Hall alarm set with no hall booking -> hall ice; office booking continues.
    ctrl, hass = make_controller()
    on(hass, E["alarm_main"])
    booking(ctrl, ZB)
    motion(ctrl, "office")
    run(ctrl.async_reconcile())
    assert ctrl.applied[ZA] == PRESET_ICE
    assert ctrl.applied[ZB] == PRESET_COMFORT


def test_both_bookings_all_zones_active():
    ctrl, _ = make_controller()
    booking(ctrl, ZA)
    booking(ctrl, ZB)
    run(ctrl.async_reconcile())
    assert ctrl.applied[ZA] == PRESET_ECO  # no motion -> eco
    assert ctrl.applied[ZB] == PRESET_ECO
    assert ctrl.applied["shared"] == PRESET_ECO
