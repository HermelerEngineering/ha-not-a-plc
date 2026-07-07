"""The Not a PLC integration: a native, cyclic ladder-style logic engine.

Phase 0 loads a bundled demo program and drives one coil entity from it. Later
phases replace the bundled program with a user-editable one (stored in
``.storage``, produced by the DSL importer and the graphical editor).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.typing import ConfigType

from . import websocket_api
from .const import DATA_COORDINATOR, DEFAULT_PROGRAM_FILE, DOMAIN
from .coordinator import LadderCoordinator
from .engine import Program, ProgramError

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR]


def _load_default_program() -> Program:
    """Load the bundled phase-0 demo program from disk."""
    path = Path(__file__).parent / "programs" / DEFAULT_PROGRAM_FILE
    data = json.loads(path.read_text(encoding="utf-8"))
    return Program.from_dict(data)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the read-only status-view websocket commands once per install."""
    websocket_api.async_register(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Not a PLC from a config entry."""
    try:
        program = await hass.async_add_executor_job(_load_default_program)
    except (OSError, ProgramError, ValueError) as err:
        raise ConfigEntryNotReady(f"could not load program: {err}") from err

    coordinator = LadderCoordinator(hass, program, entry.entry_id)
    await coordinator.async_load_retained()
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {DATA_COORDINATOR: coordinator}
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    coordinator: LadderCoordinator = hass.data[DOMAIN][entry.entry_id][DATA_COORDINATOR]
    await coordinator.async_save_retained()

    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unloaded
