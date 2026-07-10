"""The Scout Hut Heating integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import ScoutController

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SWITCH,
    Platform.TEXT,
    Platform.BUTTON,
    Platform.BINARY_SENSOR,
    Platform.SENSOR,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Scout Hut Heating from a config entry."""
    controller = ScoutController(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = controller

    # Forward to the platforms first so the tunable input entities (number,
    # select, switch, text) register themselves with the controller before the
    # first reconcile reads their values.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    controller.async_start()

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        # Only tear the controller down when the platforms really unloaded —
        # otherwise live entities would be left driving a stopped controller.
        controller: ScoutController | None = hass.data[DOMAIN].pop(entry.entry_id, None)
        if controller is not None:
            controller.async_stop()
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)
