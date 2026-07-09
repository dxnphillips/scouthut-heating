"""Boost buttons."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, ZONE_A, ZONE_B
from .coordinator import ScoutController
from .entity import ScoutEntity

# key -> (friendly name, icon, action factory)
BUTTONS: dict[str, tuple[str, str, str, str]] = {
    "boost_zone_a": ("Boost hall", "mdi:fire", "boost", ZONE_A),
    "boost_zone_b": ("Boost office", "mdi:fire", "boost", ZONE_B),
    "cancel_boost_zone_a": ("Cancel hall boost", "mdi:fire-off", "cancel", ZONE_A),
    "cancel_boost_zone_b": ("Cancel office boost", "mdi:fire-off", "cancel", ZONE_B),
}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the boost buttons."""
    controller: ScoutController = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(ScoutButton(controller, key) for key in BUTTONS)


class ScoutButton(ScoutEntity, ButtonEntity):
    """Fires a boost or cancels one on the controller."""

    def __init__(self, controller: ScoutController, key: str) -> None:
        super().__init__(controller, key)
        name, icon, self._action, self._zone = BUTTONS[key]
        self._attr_name = name
        self._attr_icon = icon

    async def async_press(self) -> None:
        if self._action == "boost":
            await self._controller.async_boost(self._zone)
        else:
            await self._controller.async_cancel_boost(self._zone)
