"""Websocket API: the bridge between the engine and the frontend status view.

Phase 2 exposes two read-only commands, both consumed by the Lovelace monitor
card:

* ``not_a_plc/get_program`` — returns the canonical IR (``Program.to_dict``) so
  the card can draw the rungs.
* ``not_a_plc/subscribe_state`` — pushes the full process image (inputs, memory
  bits and coils) after every scan, so the card can colour "energised" elements
  live. The current image is sent once immediately on subscribe.

Both commands operate on the single Not a PLC instance; there is exactly one
config entry, so we resolve its coordinator from ``hass.data``.
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
    websocket_api.async_register_command(hass, ws_get_program)
    websocket_api.async_register_command(hass, ws_subscribe_state)


def _get_coordinator(hass: HomeAssistant) -> LadderCoordinator | None:
    """Return the coordinator of the single instance, or None if not set up."""
    entries = hass.data.get(DOMAIN)
    if not entries:
        return None
    for data in entries.values():
        coordinator = data.get(DATA_COORDINATOR)
        if isinstance(coordinator, LadderCoordinator):
            return coordinator
    return None


@websocket_api.websocket_command({vol.Required("type"): "not_a_plc/get_program"})
@callback
def ws_get_program(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return the canonical program IR for the running instance."""
    coordinator = _get_coordinator(hass)
    if coordinator is None:
        connection.send_error(msg["id"], ERR_NOT_LOADED, "Not a PLC is not set up")
        return
    connection.send_result(msg["id"], {"program": coordinator.program.to_dict()})


@websocket_api.websocket_command({vol.Required("type"): "not_a_plc/subscribe_state"})
@callback
def ws_subscribe_state(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Stream the process image, but only when it actually changes.

    The scan runs at a fixed cadence (e.g. 2 Hz), yet most cycles change nothing.
    Forwarding the image every cycle floods the websocket connection and HA
    eventually drops it ("Client unable to keep up with pending messages"). So we
    push the current image once on subscribe, then only on an actual change. When
    every value is static the event rate is ~0.
    """
    coordinator = _get_coordinator(hass)
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
