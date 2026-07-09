"""Shared base entity for the Scout Hut Heating integration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.helpers.device_info import DeviceInfo

from .const import DOMAIN

if TYPE_CHECKING:
    from .coordinator import ScoutController


class ScoutEntity:
    """Mixin providing a shared device and a stable unique id."""

    _attr_has_entity_name = True

    def __init__(self, controller: "ScoutController", key: str) -> None:
        """Initialise the shared entity attributes."""
        self._controller = controller
        self._key = key
        self._attr_unique_id = f"{controller.entry.entry_id}_{key}"

    @property
    def device_info(self) -> DeviceInfo:
        """Return the single device that groups every entity."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._controller.entry.entry_id)},
            name="Scout Hut Heating",
            manufacturer="Pelsall Scout Hut",
            model="Heating controller",
        )
