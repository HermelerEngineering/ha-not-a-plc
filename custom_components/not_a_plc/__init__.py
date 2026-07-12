"""The Not-a-PLC integration: a native, cyclic ladder-style logic engine.

Each config entry is one independent "service": its own device, program,
entities and scan loop. A service owns its program canonically in ``.storage``
(seeded on creation from a bundled starter chosen in the config flow); the
graphical editor will later write to that same per-entry store. Until then the
program is the seeded starter.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from . import websocket_api
from .const import (
    BUNDLED_PROGRAMS,
    CONF_SCAN_INTERVAL,
    CONF_STARTER,
    DATA_COORDINATOR,
    DEFAULT_PROGRAM_FILE,
    DOMAIN,
    SERVICE_SOFT_CAP,
    STORAGE_KEY_PREFIX,
    STORAGE_PROGRAM_PREFIX,
    STORAGE_VERSION,
)
from .coordinator import LadderCoordinator
from .engine import Program, ProgramError

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR]


def _load_default_program() -> Program:
    """Load the bundled default starter program from disk."""
    return _load_bundled_file(DEFAULT_PROGRAM_FILE)


def _load_bundled_file(filename: str) -> Program:
    path = Path(__file__).parent / "programs" / filename
    data = json.loads(path.read_text(encoding="utf-8"))
    return Program.from_dict(data)


def _load_starter_program(starter_id: str | None) -> Program:
    """Load the bundled starter for ``starter_id`` (falls back to the default)."""
    entry = BUNDLED_PROGRAMS.get(starter_id or "")
    if entry is None:
        return _load_default_program()
    return _load_bundled_file(entry[1])


def _program_store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    return Store(hass, STORAGE_VERSION, f"{STORAGE_PROGRAM_PREFIX}.{entry_id}")


async def _async_load_program(hass: HomeAssistant, entry: ConfigEntry) -> Program:
    """Return the entry's program from .storage, seeding it once if absent.

    On first setup the store is empty, so we seed it from the starter chosen in
    the config flow (baking in the chosen scan-interval preset) and persist it as
    the canonical program. Later setups read straight from the store.
    """
    store = _program_store(hass, entry.entry_id)
    stored = await store.async_load()
    if stored is not None:
        return Program.from_dict(stored)

    starter_id = entry.data.get(CONF_STARTER)
    program = await hass.async_add_executor_job(_load_starter_program, starter_id)
    interval = entry.data.get(CONF_SCAN_INTERVAL)
    if isinstance(interval, int) and interval > 0:
        program.scan_interval_ms = interval
    await store.async_save(program.to_dict())
    return program


def _warn_on_scan_load(hass: HomeAssistant) -> None:
    """Advisory warning when many services run at once (no hard limit)."""
    count = len(hass.data.get(DOMAIN, {}))
    if count >= SERVICE_SOFT_CAP:
        _LOGGER.warning(
            "There are now %d Not-a-PLC services running; each adds a scan loop. "
            "Consider slower scan intervals if Home Assistant feels loaded.",
            count,
        )


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the read-only status-view websocket commands once per install."""
    websocket_api.async_register(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up one Not-a-PLC service from a config entry."""
    try:
        program = await _async_load_program(hass, entry)
    except (OSError, ProgramError, ValueError) as err:
        raise ConfigEntryNotReady(f"could not load program: {err}") from err

    coordinator = LadderCoordinator(hass, program, entry.entry_id)
    await coordinator.async_load_retained()
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {DATA_COORDINATOR: coordinator}
    _warn_on_scan_load(hass)
    # Reload the service when its options change (input bindings / scan interval).
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry so it re-reads its program/interval from .storage."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    coordinator: LadderCoordinator = hass.data[DOMAIN][entry.entry_id][DATA_COORDINATOR]
    await coordinator.async_save_retained()

    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete the service's per-entry .storage (program + retained bits)."""
    await _program_store(hass, entry.entry_id).async_remove()
    await Store(
        hass, STORAGE_VERSION, f"{STORAGE_KEY_PREFIX}.{entry.entry_id}"
    ).async_remove()
