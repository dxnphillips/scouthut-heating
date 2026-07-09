"""ECO keyword blocklist text helper."""

from __future__ import annotations

from homeassistant.components.text import TextEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DEFAULT_ECO_KEYWORDS, DOMAIN
from .coordinator import ScoutController
from .entity import ScoutEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the ECO keyword text helper."""
    controller: ScoutController = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([ScoutEcoKeywords(controller)])


class ScoutEcoKeywords(ScoutEntity, RestoreEntity, TextEntity):
    """Comma-separated ECO keyword blocklist used to pick eco vs comfort."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:format-list-bulleted"
    _attr_name = "ECO keyword blocklist"
    _attr_native_max = 255

    def __init__(self, controller: ScoutController) -> None:
        super().__init__(controller, "eco_keywords")
        self._attr_native_value = DEFAULT_ECO_KEYWORDS

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None and last.state not in (
            None,
            "unknown",
            "unavailable",
        ):
            self._attr_native_value = last.state
        self._controller.register_text(self._key, self)

    def restore_default(self) -> None:
        """Reset to the built-in default (used by the reset button)."""
        self._attr_native_value = DEFAULT_ECO_KEYWORDS
        self.async_write_ha_state()

    async def async_set_value(self, value: str) -> None:
        self._attr_native_value = value
        self.async_write_ha_state()
        self._controller.async_request_reconcile()
