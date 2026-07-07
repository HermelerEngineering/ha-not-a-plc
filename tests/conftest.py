"""Shared pytest configuration.

The suite has two tiers:

* **Pure engine tests** use only the standard library (plus ``jsonschema``) and
  must run without Home Assistant installed — this enforces the invariant that
  ``custom_components/not_a_plc/engine`` is HA-independent. They import the engine
  as a standalone top-level package (``import engine``), so they never execute the
  parent ``custom_components/not_a_plc/__init__.py``, which imports Home Assistant.
* **Integration tests** need ``pytest-homeassistant-custom-component`` (and thus
  Home Assistant). When HA is not installed we load neither the plugin nor its
  fixtures, and we skip collecting those modules, so the pure tier still runs.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# Expose the pure engine as a standalone ``engine`` package for the engine tests,
# without importing the HA-dependent parent package.
_ENGINE_PARENT = Path(__file__).parent.parent / "custom_components" / "not_a_plc"
if str(_ENGINE_PARENT) not in sys.path:
    sys.path.insert(0, str(_ENGINE_PARENT))

_HA_AVAILABLE = (
    importlib.util.find_spec("pytest_homeassistant_custom_component") is not None
)

if _HA_AVAILABLE:
    pytest_plugins = "pytest_homeassistant_custom_component"

    @pytest.fixture(autouse=True)
    def auto_enable_custom_integrations(enable_custom_integrations):
        """Allow Home Assistant to load the ``not_a_plc`` integration in tests."""
        yield

else:
    # Home Assistant is not installed: skip the integration modules so the pure
    # engine + schema tests still run locally.
    collect_ignore = [
        "test_init.py",
        "test_config_flow.py",
        "test_integration.py",
        "test_websocket.py",
    ]
