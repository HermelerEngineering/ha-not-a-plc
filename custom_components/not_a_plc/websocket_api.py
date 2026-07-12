"""Websocket API: the bridge between the engine and the frontend.

Read-only status-view commands (phase 2/2A), consumed by the monitor card:

* ``not_a_plc/list_services`` — the running services (``entry_id`` + name) so the
  card can offer a service selector.
* ``not_a_plc/get_program`` — the canonical IR of one service, to draw the rungs.
* ``not_a_plc/subscribe_state`` — the process image of one service, pushed on each
  actual change (see the change-detection note below).

Editing commands (phase 4.0), so a program becomes user-owned:

* ``not_a_plc/save_program`` — validate an incoming IR and make it the service's
  canonical ``.storage`` program, then reload the service.
* ``not_a_plc/get_program_text`` / ``not_a_plc/save_program_text`` — the same via
  the lossless text DSL (for YAML/git export & import).

Each command optionally takes an ``entry_id`` to target a specific service. When
omitted, the first running service is used (handy with a single instance).
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store

from .const import (
    DATA_COORDINATOR,
    DOMAIN,
    STORAGE_PROGRAM_PREFIX,
    STORAGE_VERSION,
)
from .coordinator import LadderCoordinator
from .engine import Program, ProgramError, program_from_text, program_to_text

ERR_NOT_LOADED = "not_loaded"
ERR_INVALID_PROGRAM = "invalid_program"


@callback
def async_register(hass: HomeAssistant) -> None:
    """Register the Not-a-PLC websocket commands (called once from setup)."""
    websocket_api.async_register_command(hass, ws_list_services)
    websocket_api.async_register_command(hass, ws_get_program)
    websocket_api.async_register_command(hass, ws_subscribe_state)
    websocket_api.async_register_command(hass, ws_save_program)
    websocket_api.async_register_command(hass, ws_get_program_text)
    websocket_api.async_register_command(hass, ws_save_program_text)


def _resolve_entry_id(hass: HomeAssistant, entry_id: str | None = None) -> str | None:
    """Return a running service's entry_id (the given one, or the first)."""
    entries = hass.data.get(DOMAIN)
    if not entries:
        return None
    if entry_id is not None:
        return entry_id if entry_id in entries else None
    return next(iter(entries), None)


async def async_apply_program(
    hass: HomeAssistant, entry_id: str, program: Program
) -> None:
    """Persist a validated program as a service's canonical program and reload it.

    Writing to the same ``.storage`` key the config flow seeds means the editor and
    the seeded starter share one source of truth; the reload re-reads it.
    """
    store: Store[dict[str, Any]] = Store(
        hass, STORAGE_VERSION, f"{STORAGE_PROGRAM_PREFIX}.{entry_id}"
    )
    await store.async_save(program.to_dict())
    await hass.config_entries.async_reload(entry_id)


def _get_coordinator(
    hass: HomeAssistant, entry_id: str | None = None
) -> LadderCoordinator | None:
    """Return one service's coordinator, or None if it isn't set up.

    With ``entry_id`` the specific service is returned; without it, the first
    running service (convenient for a single instance).
    """
    entries = hass.data.get(DOMAIN)
    if not entries:
        return None
    if entry_id is not None:
        data = entries.get(entry_id)
        coordinator = data.get(DATA_COORDINATOR) if data else None
        return coordinator if isinstance(coordinator, LadderCoordinator) else None
    for data in entries.values():
        coordinator = data.get(DATA_COORDINATOR)
        if isinstance(coordinator, LadderCoordinator):
            return coordinator
    return None


@websocket_api.websocket_command({vol.Required("type"): "not_a_plc/list_services"})
@callback
def ws_list_services(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return the running services as ``{entry_id, name}`` for the card selector."""
    services: list[dict[str, str]] = []
    for entry_id in hass.data.get(DOMAIN, {}):
        entry = hass.config_entries.async_get_entry(entry_id)
        services.append(
            {"entry_id": entry_id, "name": entry.title if entry else entry_id}
        )
    connection.send_result(msg["id"], {"services": services})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "not_a_plc/get_program",
        vol.Optional("entry_id"): str,
    }
)
@callback
def ws_get_program(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return the canonical program IR for one service."""
    coordinator = _get_coordinator(hass, msg.get("entry_id"))
    if coordinator is None:
        connection.send_error(msg["id"], ERR_NOT_LOADED, "Not-a-PLC is not set up")
        return
    connection.send_result(msg["id"], {"program": coordinator.program.to_dict()})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "not_a_plc/subscribe_state",
        vol.Optional("entry_id"): str,
    }
)
@callback
def ws_subscribe_state(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Stream one service's process image, but only when it actually changes.

    The scan runs at a fixed cadence (e.g. 2 Hz), yet most cycles change nothing.
    Forwarding the image every cycle floods the websocket connection and HA
    eventually drops it ("Client unable to keep up with pending messages"). So we
    push the current image once on subscribe, then only on an actual change. When
    every value is static the event rate is ~0.
    """
    coordinator = _get_coordinator(hass, msg.get("entry_id"))
    if coordinator is None:
        connection.send_error(msg["id"], ERR_NOT_LOADED, "Not-a-PLC is not set up")
        return

    # Last image sent on *this* subscription. ``None`` (sentinel) is distinct from
    # an empty/false-valued image, so the first push always goes out.
    last_sent: dict[str, Any] | None = None

    @callback
    def forward_state() -> None:
        nonlocal last_sent
        image = coordinator.state_image()
        if image == last_sent:
            return
        last_sent = image
        connection.send_message(
            websocket_api.event_message(msg["id"], {"state": image})
        )

    connection.subscriptions[msg["id"]] = coordinator.async_add_listener(forward_state)
    connection.send_result(msg["id"])
    forward_state()


@websocket_api.websocket_command(
    {
        vol.Required("type"): "not_a_plc/save_program",
        vol.Optional("entry_id"): str,
        vol.Required("program"): dict,
    }
)
@websocket_api.async_response
async def ws_save_program(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Validate an incoming IR and make it the service's canonical program."""
    entry_id = _resolve_entry_id(hass, msg.get("entry_id"))
    if entry_id is None:
        connection.send_error(msg["id"], ERR_NOT_LOADED, "Not-a-PLC is not set up")
        return
    try:
        program = Program.from_dict(msg["program"])
    except ProgramError as err:
        connection.send_error(msg["id"], ERR_INVALID_PROGRAM, str(err))
        return
    await async_apply_program(hass, entry_id, program)
    connection.send_result(msg["id"])


@websocket_api.websocket_command(
    {
        vol.Required("type"): "not_a_plc/get_program_text",
        vol.Optional("entry_id"): str,
    }
)
@callback
def ws_get_program_text(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return the service's program as lossless DSL text (for YAML/git export)."""
    coordinator = _get_coordinator(hass, msg.get("entry_id"))
    if coordinator is None:
        connection.send_error(msg["id"], ERR_NOT_LOADED, "Not-a-PLC is not set up")
        return
    connection.send_result(msg["id"], {"text": program_to_text(coordinator.program)})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "not_a_plc/save_program_text",
        vol.Optional("entry_id"): str,
        vol.Required("text"): str,
    }
)
@websocket_api.async_response
async def ws_save_program_text(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Parse DSL text into a program and make it the service's canonical program."""
    entry_id = _resolve_entry_id(hass, msg.get("entry_id"))
    if entry_id is None:
        connection.send_error(msg["id"], ERR_NOT_LOADED, "Not-a-PLC is not set up")
        return
    try:
        program = program_from_text(msg["text"])
    except ProgramError as err:
        connection.send_error(msg["id"], ERR_INVALID_PROGRAM, str(err))
        return
    await async_apply_program(hass, entry_id, program)
    connection.send_result(msg["id"])
