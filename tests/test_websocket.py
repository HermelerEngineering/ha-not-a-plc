"""Phase-2 websocket tests (require pytest-homeassistant-custom-component).

We exercise the command handlers directly against a fake connection rather than
through ``hass_ws_client``. That keeps the tests focused on our logic and avoids
starting the real HTTP server (whose executor-shutdown thread otherwise trips
pytest-homeassistant-custom-component's lingering-resource teardown check).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components import not_a_plc as integration
from custom_components.not_a_plc.const import DOMAIN
from custom_components.not_a_plc.engine import Program
from custom_components.not_a_plc.websocket_api import (
    ERR_NOT_LOADED,
    ws_get_program,
    ws_subscribe_state,
)

# A minimal program: one BOOL input drives one coil (= mode).
_PROGRAM = {
    "meta": {"name": "WS demo"},
    "tags": {
        "i": {"kind": "input", "source": "binary_sensor.trigger"},
        "o": {"kind": "coil"},
    },
    "networks": [
        {
            "id": "n",
            "rungs": [
                {
                    "id": "r",
                    "series": [{"type": "contact", "tag": "i"}],
                    "coils": [{"type": "coil", "tag": "o"}],
                }
            ],
        }
    ],
}


class FakeConnection:
    """Captures what a websocket command handler sends back."""

    def __init__(self) -> None:
        self.results: dict[int, Any] = {}
        self.errors: dict[int, tuple[str, str]] = {}
        self.events: list[Any] = []
        self.subscriptions: dict[int, Any] = {}

    def send_result(self, msg_id: int, result: Any = None) -> None:
        self.results[msg_id] = result

    def send_error(self, msg_id: int, code: str, message: str) -> None:
        self.errors[msg_id] = (code, message)

    def send_message(self, message: Any) -> None:
        self.events.append(message)


def _use_program(monkeypatch: pytest.MonkeyPatch, data: dict) -> None:
    program = Program.from_dict(data)
    monkeypatch.setattr(integration, "_load_default_program", lambda: program)


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _tick(hass: HomeAssistant) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1))
    await hass.async_block_till_done()


async def test_get_program_returns_ir(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_program(monkeypatch, _PROGRAM)
    hass.states.async_set("binary_sensor.trigger", "off")
    await _setup(hass)

    conn = FakeConnection()
    ws_get_program(hass, conn, {"id": 1, "type": "not_a_plc/get_program"})

    program = conn.results[1]["program"]
    assert program["meta"]["name"] == "WS demo"
    assert program["tags"]["i"]["kind"] == "input"
    assert program["tags"]["i"]["source"] == "binary_sensor.trigger"
    assert program["networks"][0]["rungs"][0]["coils"][0]["tag"] == "o"


async def test_get_program_errors_when_not_loaded(hass: HomeAssistant) -> None:
    conn = FakeConnection()
    ws_get_program(hass, conn, {"id": 2, "type": "not_a_plc/get_program"})

    assert conn.errors[2][0] == ERR_NOT_LOADED
    assert 2 not in conn.results


async def test_subscribe_state_sends_only_on_change(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_program(monkeypatch, _PROGRAM)
    hass.states.async_set("binary_sensor.trigger", "off")
    await _setup(hass)

    conn = FakeConnection()
    ws_subscribe_state(hass, conn, {"id": 5, "type": "not_a_plc/subscribe_state"})

    # Subscribe acknowledged, and the current image pushed immediately.
    assert 5 in conn.results
    assert len(conn.events) == 1
    assert conn.events[0]["event"]["state"] == {"i": False, "o": False}

    # An unchanged scan must NOT push anything (this is the flood fix).
    await _tick(hass)
    assert len(conn.events) == 1

    # Drive the input on: exactly one event for the change.
    hass.states.async_set("binary_sensor.trigger", "on")
    await _tick(hass)
    assert len(conn.events) == 2
    assert conn.events[1]["event"]["state"] == {"i": True, "o": True}

    # Steady again across several scans: still no new events.
    await _tick(hass)
    await _tick(hass)
    assert len(conn.events) == 2

    # Unsubscribing removes the coordinator listener entirely.
    conn.subscriptions[5]()
    hass.states.async_set("binary_sensor.trigger", "off")
    await _tick(hass)
    assert len(conn.events) == 2
