"""Selects owned by the integration (boost duration, cooling changeover)."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN, SELECT_DEFS, SELECT_ICONS, SELECT_NAMES
from .coordinator import ScoutController
from .entity import ScoutEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the selects."""
    controller: ScoutController = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(ScoutSelect(controller, key) for key in SELECT_DEFS)


class ScoutSelect(ScoutEntity, RestoreEntity, SelectEntity):
    """A restorable option chooser that drives the reconciler."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, controller: ScoutController, key: str) -> None:
        super().__init__(controller, key)
        options, default = SELECT_DEFS[key]
        self._attr_name = SELECT_NAMES[key]
        self._attr_icon = SELECT_ICONS.get(key)
        self._attr_options = options
        self._attr_current_option = default

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) and last.state in self._attr_options:
            self._attr_current_option = last.state
        self._controller.register_select(self._key, self)

    def restore_default(self) -> None:
        """Reset to the built-in default (used by the reset button)."""
        self._attr_current_option = SELECT_DEFS[self._key][1]
        self.async_write_ha_state()

    async def async_select_option(self, option: str) -> None:
        self._attr_current_option = option
        self.async_write_ha_state()
        self._controller.async_request_reconcile()
