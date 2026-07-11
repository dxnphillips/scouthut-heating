"""Tunable number helpers owned by the integration."""

from __future__ import annotations

from homeassistant.components.number import NumberMode, RestoreNumber
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, NUMBER_DEFS, NUMBER_ICONS
from .coordinator import ScoutController
from .entity import ScoutEntity

NAMES: dict[str, str] = {
    "preheat_minutes": "Pre-heat lead time (max)",
    "zone_a_warmup_rate": "Hall learned warm-up rate",
    "zone_a_warmup_rate_fans": "Hall learned warm-up rate (fans running)",
    "zone_b_warmup_rate": "Office learned warm-up rate",
    "zone_a_heatloss_pct": "Hall learned heat loss (% of gap/h)",
    "zone_b_heatloss_pct": "Office learned heat loss (% of gap/h)",
    "motion_timeout_minutes": "No-motion eco timeout",
    "door_ice_minutes": "Door: drop to ice after",
    "window_ice_minutes": "Window: drop to ice after",
    "seasonal_lockout_temp": "Seasonal lockout: 3-day average threshold",
    "hall_comfort_temp": "Hall comfort temperature",
    "hall_eco_temp": "Hall eco temperature",
    "hall_eco_low_temp": "Hall eco-low temperature",
    "water_preheat_minutes": "Water heater pre-heat lead time",
    "water_motion_keepalive_minutes": "Water heater keep-on after motion",
    "fan_dt_on": "Fan: ceiling-floor ΔT to start",
    "fan_dt_off": "Fan: ceiling-floor ΔT to stop",
    "fan_min_run_minutes": "Fan: minimum run time",
    "fan_min_off_minutes": "Fan: minimum off time",
    "fan_sensor_stale_minutes": "Fan: sensor stale after",
    "cooling_temp_high": "Cooling: warm-enough temperature",
    "cooling_mix_max_temp": "Cooling: max useful breeze temperature",
    "heat_demand_watts": "Fan: heat-demand power threshold",
    "fan_recirc_max_floor_temp": "Fan: recirculate until floor reaches",
}

HALL_TEMP_KEYS = ("hall_comfort_temp", "hall_eco_temp", "hall_eco_low_temp")


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the number helpers."""
    controller: ScoutController = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(ScoutNumber(controller, key) for key in NUMBER_DEFS)


class ScoutNumber(ScoutEntity, RestoreNumber):
    """A restorable, dashboard-tunable number."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.SLIDER

    def __init__(self, controller: ScoutController, key: str) -> None:
        super().__init__(controller, key)
        min_v, max_v, step, default, unit = NUMBER_DEFS[key]
        self._attr_name = NAMES[key]
        self._attr_native_min_value = min_v
        self._attr_native_max_value = max_v
        self._attr_native_step = step
        self._attr_native_unit_of_measurement = unit
        self._attr_native_value = float(default)
        self._attr_icon = NUMBER_ICONS.get(key)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        data = await self.async_get_last_number_data()
        if data is not None and data.native_value is not None:
            # Clamp into the current bounds: an upgrade may have tightened them
            # (e.g. to the ranges the Rointe number entities accept) and a
            # restored out-of-range value would otherwise survive untouched.
            self._attr_native_value = min(
                self._attr_native_max_value,
                max(self._attr_native_min_value, float(data.native_value)),
            )
        self._controller.register_number(self._key, self)

    def restore_default(self) -> None:
        """Reset to the built-in default (used by the reset button)."""
        self._attr_native_value = float(NUMBER_DEFS[self._key][3])
        self.async_write_ha_state()

    def write_value(self, value: float) -> None:
        """Quietly store a controller-computed value (learning updates).

        Unlike async_set_native_value this triggers no side effects and no
        reconcile — the caller is the reconciler itself.
        """
        self._attr_native_value = float(value)
        self.async_write_ha_state()

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = value
        self.async_write_ha_state()
        if self._key in HALL_TEMP_KEYS:
            await self._controller.async_hall_temps_changed()
        elif self._key == "seasonal_lockout_temp":
            await self._controller.async_seasonal_recheck()
        self._controller.async_request_reconcile()
