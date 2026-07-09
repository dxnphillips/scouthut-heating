"""User-controllable switches owned by the integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN, SWITCH_DEFS, SWITCH_ICONS
from .coordinator import ScoutController
from .entity import ScoutEntity

NAMES: dict[str, str] = {
    "zone_a_automation_enabled": "Hall automation enabled",
    "zone_b_automation_enabled": "Office automation enabled",
    "zone_a_occupied_override": "Hall occupied override",
    "zone_b_occupied_override": "Office occupied override",
    "water_manual_override": "Water heater manual override",
}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the switches."""
    controller: ScoutController = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(ScoutSwitch(controller, key) for key in SWITCH_DEFS)


class ScoutSwitch(ScoutEntity, RestoreEntity, SwitchEntity):
    """A restorable on/off control that drives the reconciler."""

    def __init__(self, controller: ScoutController, key: str) -> None:
        super().__init__(controller, key)
        self._attr_name = NAMES[key]
        self._attr_icon = SWITCH_ICONS.get(key)
        self._attr_is_on = SWITCH_DEFS[key]

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            self._attr_is_on = last.state == "on"
        self._controller.register_switch(self._key, self)

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._attr_is_on = True
        self.async_write_ha_state()
        self._controller.async_request_reconcile()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._attr_is_on = False
        self.async_write_ha_state()
        self._controller.async_request_reconcile()
