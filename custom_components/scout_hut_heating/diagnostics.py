"""Diagnostics download for the Scout Hut Heating integration.

Settings → Devices & Services → Scout Hut Heating → ⋮ → Download diagnostics
produces one JSON file with the current tunables (against their defaults),
the learned rates, a live sensor snapshot and the rolling audit-event log —
everything needed to check the control algorithms against the building's
real behaviour.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import ScoutController


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    controller: ScoutController = hass.data[DOMAIN][entry.entry_id]
    return controller.diagnostics_data()
