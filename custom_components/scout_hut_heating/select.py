"""Boost-duration select owned by the integration."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import BOOST_DEFAULT, BOOST_OPTIONS, DOMAIN
from .coordinator import ScoutController
from .entity import ScoutEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the boost-duration select."""
    controller: ScoutController = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([ScoutBoostDuration(controller)])


class ScoutBoostDuration(ScoutEntity, RestoreEntity, SelectEntity):
    """Selectable boost duration used by the boost buttons."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:fire"
    _attr_name = "Boost duration"
    _attr_options = BOOST_OPTIONS

    def __init__(self, controller: ScoutController) -> None:
        super().__init__(controller, "boost_duration")
        self._attr_current_option = BOOST_DEFAULT

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) and last.state in BOOST_OPTIONS:
            self._attr_current_option = last.state
        self._controller.register_select(self._key, self)

    async def async_select_option(self, option: str) -> None:
        self._attr_current_option = option
        self.async_write_ha_state()
