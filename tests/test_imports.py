"""Smoke test: every module imports cleanly (guards against bad import paths)."""

import importlib

MODULES = [
    "const",
    "entity",
    "coordinator",
    "number",
    "select",
    "switch",
    "text",
    "button",
    "binary_sensor",
    "sensor",
    "config_flow",
    "__init__",
]


def test_all_modules_import():
    for name in MODULES:
        importlib.import_module(f"custom_components.scout_hut_heating.{name}")
