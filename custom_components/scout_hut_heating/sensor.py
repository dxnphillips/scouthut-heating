"""Diagnostic sensors reporting the applied state of each zone."""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, ZONE_A, ZONE_B
from .coordinator import SIGNAL_UPDATE, ScoutController
from .entity import ScoutEntity

# key -> (name, icon, state getter)
SENSORS: dict[str, tuple[str, str, Callable[[ScoutController], str | None]]] = {
    "zone_a_status": ("Hall preset", "mdi:radiator", lambda c: c.applied[ZONE_A]),
    "zone_b_status": ("Office preset", "mdi:radiator", lambda c: c.applied[ZONE_B]),
    "shared_status": ("Shared zone preset", "mdi:radiator", lambda c: c.applied["shared"]),
    "water_status": (
        "Water heater",
        "mdi:water-boiler",
        lambda c: None if c.water_on is None else ("on" if c.water_on else "off"),
    ),
    "zone_a_expected_preset": (
        "Hall expected preset",
        "mdi:thermometer-check",
        lambda c: c.expected_preset[ZONE_A],
    ),
    "zone_b_expected_preset": (
        "Office expected preset",
        "mdi:thermometer-check",
        lambda c: c.expected_preset[ZONE_B],
    ),
    "fan_mode": ("Ceiling fan mode", "mdi:ceiling-fan", lambda c: c.fan_mode),
    "fan_direction": ("Ceiling fan direction", "mdi:fan", lambda c: c.fan_direction),
    "fan_delta_t": (
        "Ceiling-floor ΔT",
        "mdi:thermometer-lines",
        lambda c: None if c.fan_dt is None else str(round(c.fan_dt, 1)),
    ),
}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the diagnostic sensors."""
    controller: ScoutController = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(ScoutSensor(controller, key) for key in SENSORS)


class ScoutSensor(ScoutEntity, SensorEntity):
    """Reflects an applied preset / on-off value from the controller."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, controller: ScoutController, key: str) -> None:
        super().__init__(controller, key)
        name, icon, self._getter = SENSORS[key]
        self._attr_name = name
        self._attr_icon = icon

    @property
    def native_value(self) -> str | None:
        return self._getter(self._controller)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_UPDATE, self._handle_update)
        )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()
