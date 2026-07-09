"""Config and options flow for Scout Hut Heating."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_ALARM_MAIN,
    CONF_ALARM_OFFICE,
    CONF_CALENDAR_HALL,
    CONF_CALENDAR_OFFICE,
    CONF_HALL_CLIMATES,
    CONF_HALL_COMFORT_NUMBERS,
    CONF_HALL_ECO_NUMBERS,
    CONF_INTERNAL_DOOR,
    CONF_MOTION_FEMALE,
    CONF_MOTION_GENTS,
    CONF_MOTION_HALL,
    CONF_MOTION_KITCHEN,
    CONF_MOTION_OFFICE,
    CONF_OFFICE_CLIMATES,
    CONF_REALFEEL,
    CONF_SHARED_CLIMATES,
    CONF_SHARED_WINDOWS,
    CONF_WATER_SWITCH,
    CONF_WEATHER,
    CONF_ZONE_A_DOORS,
    CONF_ZONE_A_WINDOWS,
    CONF_ZONE_B_DOORS,
    CONF_ZONE_B_WINDOWS,
    DOMAIN,
)

BINARY = ["binary_sensor", "input_boolean"]


def _sel(domain: str | list[str], multiple: bool = False) -> selector.EntitySelector:
    return selector.EntitySelector(
        selector.EntitySelectorConfig(domain=domain, multiple=multiple)
    )


def _add(
    schema: dict,
    key: str,
    required: bool,
    sel: selector.EntitySelector,
    defaults: dict[str, Any],
) -> None:
    marker = vol.Required if required else vol.Optional
    if key in defaults and defaults[key] not in (None, []):
        schema[marker(key, description={"suggested_value": defaults[key]})] = sel
    else:
        schema[marker(key)] = sel


def _zones_schema(d: dict[str, Any]) -> vol.Schema:
    s: dict = {}
    _add(s, CONF_HALL_CLIMATES, True, _sel("climate", True), d)
    _add(s, CONF_OFFICE_CLIMATES, True, _sel("climate", True), d)
    _add(s, CONF_SHARED_CLIMATES, False, _sel("climate", True), d)
    _add(s, CONF_HALL_COMFORT_NUMBERS, False, _sel("number", True), d)
    _add(s, CONF_HALL_ECO_NUMBERS, False, _sel("number", True), d)
    return vol.Schema(s)


def _motion_schema(d: dict[str, Any]) -> vol.Schema:
    s: dict = {}
    _add(s, CONF_MOTION_HALL, False, _sel(BINARY), d)
    _add(s, CONF_MOTION_OFFICE, False, _sel(BINARY), d)
    _add(s, CONF_MOTION_KITCHEN, False, _sel(BINARY), d)
    _add(s, CONF_MOTION_GENTS, False, _sel(BINARY), d)
    _add(s, CONF_MOTION_FEMALE, False, _sel(BINARY), d)
    return vol.Schema(s)


def _openings_schema(d: dict[str, Any]) -> vol.Schema:
    s: dict = {}
    _add(s, CONF_ZONE_A_DOORS, False, _sel(BINARY, True), d)
    _add(s, CONF_ZONE_A_WINDOWS, False, _sel(BINARY, True), d)
    _add(s, CONF_ZONE_B_DOORS, False, _sel(BINARY, True), d)
    _add(s, CONF_ZONE_B_WINDOWS, False, _sel(BINARY, True), d)
    _add(s, CONF_SHARED_WINDOWS, False, _sel(BINARY, True), d)
    _add(s, CONF_INTERNAL_DOOR, False, _sel(BINARY), d)
    return vol.Schema(s)


def _extras_schema(d: dict[str, Any]) -> vol.Schema:
    s: dict = {}
    _add(s, CONF_CALENDAR_HALL, True, _sel("calendar"), d)
    _add(s, CONF_CALENDAR_OFFICE, True, _sel("calendar"), d)
    _add(s, CONF_WEATHER, False, _sel("weather"), d)
    _add(s, CONF_REALFEEL, False, _sel("sensor"), d)
    _add(s, CONF_ALARM_MAIN, False, _sel(BINARY), d)
    _add(s, CONF_ALARM_OFFICE, False, _sel(BINARY), d)
    _add(s, CONF_WATER_SWITCH, False, _sel("switch"), d)
    return vol.Schema(s)


class ScoutConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the initial multi-step setup."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_motion()
        return self.async_show_form(step_id="user", data_schema=_zones_schema({}))

    async def async_step_motion(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_openings()
        return self.async_show_form(step_id="motion", data_schema=_motion_schema({}))

    async def async_step_openings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_extras()
        return self.async_show_form(step_id="openings", data_schema=_openings_schema({}))

    async def async_step_extras(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return self.async_create_entry(title="Scout Hut Heating", data=self._data)
        return self.async_show_form(step_id="extras", data_schema=_extras_schema({}))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return ScoutOptionsFlow(config_entry)


class ScoutOptionsFlow(OptionsFlow):
    """Allow re-mapping entities after setup."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._entry = config_entry
        self._data: dict[str, Any] = {**config_entry.data, **config_entry.options}

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_motion()
        return self.async_show_form(step_id="init", data_schema=_zones_schema(self._data))

    async def async_step_motion(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_openings()
        return self.async_show_form(step_id="motion", data_schema=_motion_schema(self._data))

    async def async_step_openings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_extras()
        return self.async_show_form(
            step_id="openings", data_schema=_openings_schema(self._data)
        )

    async def async_step_extras(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return self.async_create_entry(title="", data=self._data)
        return self.async_show_form(step_id="extras", data_schema=_extras_schema(self._data))
