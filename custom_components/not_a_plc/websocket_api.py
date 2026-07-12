"""Websocket API: the bridge between the engine and the frontend status view.

Phase 2/2A read-only commands, consumed by the Lovelace monitor card:

* ``not_a_plc/list_services`` — the running services (``entry_id`` + name) so the
  card can offer a service selector.
* ``not_a_plc/get_program`` — the canonical IR of one service, to draw the rungs.
* ``not_a_plc/subscribe_state`` — the process image of one service, pushed on each
  actual change (see the change-detection note below).

Each command optionally takes an ``entry_id`` to target a specific service. When
omitted, the first running service is used (handy with a single instance).
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .const import DATA_COORDINATOR, DOMAIN
from .coordinator import LadderCoordinator

ERR_NOT_LOADED = "not_loaded"


@callback
def async_register(hass: HomeAssistant) -> None:
    """Register the Not a PLC websocket commands (called once from setup)."""
    websocket_api.async_register_command(hass, ws_list_services)
    websocket_api.async_register_command(hass, ws_get_program)
    websocket_api.async_register_command(hass, ws_subscribe_state)


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
        connection.send_error(msg["id"], ERR_NOT_LOADED, "Not a PLC is not set up")
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
        connection.send_error(msg["id"], ERR_NOT_LOADED, "Not a PLC is not set up")
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
