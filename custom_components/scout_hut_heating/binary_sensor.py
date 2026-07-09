"""Diagnostic binary sensors reflecting the controller's internal state."""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, ZONE_A, ZONE_B
from .coordinator import SIGNAL_UPDATE, ScoutController
from .entity import ScoutEntity

# key -> (name, icon, state getter)
SENSORS: dict[str, tuple[str, str, Callable[[ScoutController], bool]]] = {
    "seasonal_lockout_active": (
        "Seasonal lockout active",
        "mdi:leaf",
        lambda c: c.seasonal_lockout,
    ),
    "zone_a_opening_ice_active": (
        "Hall opening ice active",
        "mdi:snowflake-alert",
        lambda c: c.opening_ice[ZONE_A],
    ),
    "zone_b_opening_ice_active": (
        "Office opening ice active",
        "mdi:snowflake-alert",
        lambda c: c.opening_ice[ZONE_B],
    ),
    "shared_opening_ice_active": (
        "Shared opening ice active",
        "mdi:snowflake-alert",
        lambda c: c.opening_ice["shared"],
    ),
    "zone_a_manual_hold": (
        "Hall manual hold",
        "mdi:hand-back-right",
        lambda c: c.manual_hold[ZONE_A],
    ),
    "zone_b_manual_hold": (
        "Office manual hold",
        "mdi:hand-back-right",
        lambda c: c.manual_hold[ZONE_B],
    ),
    "zone_a_boost_active": ("Hall boost active", "mdi:fire", lambda c: c.boost_active(ZONE_A)),
    "zone_b_boost_active": ("Office boost active", "mdi:fire", lambda c: c.boost_active(ZONE_B)),
    "fan_running": ("Ceiling fan running", "mdi:ceiling-fan", lambda c: bool(c.fan_on)),
    "fan_fault_effective": (
        "Ceiling fan fault",
        "mdi:fan-alert",
        lambda c: c.fan_fault_effective,
    ),
    "fan_sensor_stale": (
        "Fan temperature sensor lost",
        "mdi:thermometer-off",
        lambda c: c.fan_sensor_stale,
    ),
    "fan_heat_demand": ("Heat demand active", "mdi:radiator", lambda c: c.heat_demand),
}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the diagnostic binary sensors."""
    controller: ScoutController = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(ScoutBinarySensor(controller, key) for key in SENSORS)


class ScoutBinarySensor(ScoutEntity, BinarySensorEntity):
    """Reflects a boolean piece of controller state."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, controller: ScoutController, key: str) -> None:
        super().__init__(controller, key)
        name, icon, self._getter = SENSORS[key]
        self._attr_name = name
        self._attr_icon = icon

    @property
    def is_on(self) -> bool:
        return bool(self._getter(self._controller))

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_UPDATE, self._handle_update)
        )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()
