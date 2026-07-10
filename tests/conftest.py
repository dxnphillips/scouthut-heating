"""Test bootstrap: install lightweight Home Assistant stubs.

The integration can't be imported without the `homeassistant` package, and a
full HA install is heavy (and unavailable in some CI images). These stubs
provide just the API surface the integration imports, so the pure control
logic can be exercised with plain pytest. Every stubbed import path is one the
integration actually uses and has been checked against the real HA source.
"""

from __future__ import annotations

import os
import sys
import types

# Make `custom_components...` importable from the repo root.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def _mod(name: str) -> types.ModuleType:
    module = types.ModuleType(name)
    sys.modules[name] = module
    return module


def _install_stubs() -> None:
    if "homeassistant" in sys.modules:
        return

    # voluptuous (config_flow) — stub only if the real package is absent.
    try:
        import voluptuous  # noqa: F401
    except ModuleNotFoundError:
        vol = _mod("voluptuous")

        class _Schema:
            def __init__(self, schema):
                self.schema = schema

        class _Marker:
            def __init__(self, key, description=None):
                self.key = key
                self.description = description

            def __hash__(self):
                return hash(self.key)

        vol.Schema = _Schema
        vol.Required = _Marker
        vol.Optional = _Marker

    _mod("homeassistant")

    core = _mod("homeassistant.core")
    core.callback = lambda f: f
    core.HomeAssistant = type("HomeAssistant", (), {})
    core.Event = type("Event", (), {})

    const = _mod("homeassistant.const")
    const.Platform = type(
        "Platform",
        (),
        {
            "NUMBER": "number",
            "SELECT": "select",
            "SWITCH": "switch",
            "TEXT": "text",
            "BUTTON": "button",
            "BINARY_SENSOR": "binary_sensor",
            "SENSOR": "sensor",
        },
    )
    const.EntityCategory = type("EntityCategory", (), {"CONFIG": "config", "DIAGNOSTIC": "diagnostic"})

    ce = _mod("homeassistant.config_entries")

    class _ConfigEntry:
        def __init__(self, data=None, options=None):
            self.data = data or {}
            self.options = options or {}
            self.entry_id = "test"

        def add_update_listener(self, cb):
            return lambda: None

        def async_on_unload(self, x):
            return None

    class _ConfigFlow:
        def __init_subclass__(cls, **kwargs):
            super().__init_subclass__()

        async def async_set_unique_id(self, uid):
            self._uid = uid

        def _abort_if_unique_id_configured(self):
            pass

        def async_show_form(self, **kw):
            return {"type": "form", **kw}

        def async_create_entry(self, **kw):
            return {"type": "create", **kw}

    ce.ConfigEntry = _ConfigEntry
    ce.ConfigFlow = _ConfigFlow
    ce.ConfigFlowResult = dict
    ce.OptionsFlow = type("OptionsFlow", (), {})

    pn = _mod("homeassistant.components.persistent_notification")
    pn.async_create = lambda *a, **k: None
    pn.async_dismiss = lambda *a, **k: None
    _mod("homeassistant.components")

    _mod("homeassistant.helpers")
    disp = _mod("homeassistant.helpers.dispatcher")
    disp.async_dispatcher_send = lambda *a, **k: None
    disp.async_dispatcher_connect = lambda *a, **k: (lambda: None)

    ev = _mod("homeassistant.helpers.event")
    ev.async_call_later = lambda *a, **k: (lambda: None)
    ev.async_track_state_change_event = lambda *a, **k: (lambda: None)
    ev.async_track_time_change = lambda *a, **k: (lambda: None)
    ev.async_track_time_interval = lambda *a, **k: (lambda: None)

    dr = _mod("homeassistant.helpers.device_registry")
    dr.DeviceInfo = lambda **k: k

    # Mutable entity-registry stub used by auto-discovery tests.
    erm = _mod("homeassistant.helpers.entity_registry")

    class _RegEntry:
        def __init__(self, entity_id, device_id, unique_id=None, config_entry_id=None):
            self.entity_id = entity_id
            self.device_id = device_id
            self.domain = entity_id.split(".")[0]
            self.unique_id = unique_id
            self.config_entry_id = config_entry_id

    class _Reg:
        def __init__(self):
            self.by_id = {}
            self.by_device = {}

        def async_get(self, entity_id):
            return self.by_id.get(entity_id)

    erm._REG = _Reg()
    erm._RegEntry = _RegEntry
    erm.async_get = lambda hass: erm._REG
    erm.async_entries_for_device = (
        lambda reg, device_id, include_disabled_entities=False: reg.by_device.get(device_id, [])
    )
    erm.async_entries_for_config_entry = lambda reg, entry_id: [
        e for e in reg.by_id.values() if getattr(e, "config_entry_id", None) == entry_id
    ]

    sel = _mod("homeassistant.helpers.selector")
    sel.EntitySelector = lambda cfg: ("sel", cfg)
    sel.EntitySelectorConfig = lambda **k: k

    epf = _mod("homeassistant.helpers.entity_platform")
    epf.AddEntitiesCallback = object

    rs = _mod("homeassistant.helpers.restore_state")

    class _RestoreEntity:
        async def async_added_to_hass(self):
            pass

        async def async_get_last_state(self):
            return None

        def async_on_remove(self, x):
            pass

        def async_write_ha_state(self):
            pass

    rs.RestoreEntity = _RestoreEntity

    util = _mod("homeassistant.util")  # noqa: F841
    dtm = _mod("homeassistant.util.dt")
    from datetime import datetime, timezone

    dtm.utcnow = lambda: datetime.now(timezone.utc)
    dtm.now = lambda: datetime.now()

    class _Ent:
        def async_write_ha_state(self):
            pass

        def async_on_remove(self, x):
            pass

        async def async_added_to_hass(self):
            pass

    class _RestoreNumber(_Ent, _RestoreEntity):
        async def async_get_last_number_data(self):
            return None

    def _comp(name, **attrs):
        module = _mod(f"homeassistant.components.{name}")
        for key, value in attrs.items():
            setattr(module, key, value)
        return module

    _comp(
        "number",
        RestoreNumber=_RestoreNumber,
        NumberMode=type("NumberMode", (), {"SLIDER": "slider", "BOX": "box"}),
    )
    _comp("select", SelectEntity=type("SelectEntity", (_Ent,), {}))
    _comp("switch", SwitchEntity=type("SwitchEntity", (_Ent,), {}))
    _comp("text", TextEntity=type("TextEntity", (_Ent,), {}))
    _comp("button", ButtonEntity=type("ButtonEntity", (_Ent,), {}))
    _comp("binary_sensor", BinarySensorEntity=type("BinarySensorEntity", (_Ent,), {}))
    _comp("sensor", SensorEntity=type("SensorEntity", (_Ent,), {}))


_install_stubs()


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_entity_registry():
    """Keep the stubbed entity registry isolated between tests."""
    from homeassistant.helpers import entity_registry as er

    er._REG.by_id = {}
    er._REG.by_device = {}
    yield
