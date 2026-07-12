"""Phase-2 websocket tests (require pytest-homeassistant-custom-component).

We exercise the command handlers directly against a fake connection rather than
through ``hass_ws_client``. That keeps the tests focused on our logic and avoids
starting the real HTTP server (whose executor-shutdown thread otherwise trips
pytest-homeassistant-custom-component's lingering-resource teardown check).
"""

from __future__ import annotations

import asyncio
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
from custom_components.not_a_plc.const import DATA_COORDINATOR, DOMAIN
from custom_components.not_a_plc.engine import Program, program_from_text
from custom_components.not_a_plc.websocket_api import (
    ERR_INVALID_PROGRAM,
    ERR_NOT_LOADED,
    ws_get_program,
    ws_get_program_text,
    ws_list_services,
    ws_save_program,
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
        self.tasks: list[asyncio.Task[Any]] = []

    def send_result(self, msg_id: int, result: Any = None) -> None:
        self.results[msg_id] = result

    def send_error(self, msg_id: int, code: str, message: str) -> None:
        self.errors[msg_id] = (code, message)

    def send_message(self, message: Any) -> None:
        self.events.append(message)

    def async_create_task(self, target: Any, *_args: Any, **_kwargs: Any) -> Any:
        # Async command handlers (@async_response) schedule their coroutine here.
        task = asyncio.ensure_future(target)
        self.tasks.append(task)
        return task

    async def async_wait(self) -> None:
        if self.tasks:
            await asyncio.gather(*self.tasks)


def _use_program(monkeypatch: pytest.MonkeyPatch, data: dict) -> None:
    program = Program.from_dict(data)
    monkeypatch.setattr(integration, "_load_default_program", lambda: program)


async def _setup(hass: HomeAssistant, title: str = "Not a PLC") -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, title=title, data={})
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


async def test_list_services_and_entry_id_targeting(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_program(monkeypatch, _PROGRAM)
    hass.states.async_set("binary_sensor.trigger", "off")
    entry_a = await _setup(hass, "Service A")
    entry_b = await _setup(hass, "Service B")

    # list_services returns both running services with their names.
    conn = FakeConnection()
    ws_list_services(hass, conn, {"id": 1, "type": "not_a_plc/list_services"})
    services = conn.results[1]["services"]
    assert {s["name"] for s in services} == {"Service A", "Service B"}
    assert {s["entry_id"] for s in services} == {entry_a.entry_id, entry_b.entry_id}

    # get_program targets a specific service by entry_id.
    conn2 = FakeConnection()
    ws_get_program(
        hass,
        conn2,
        {"id": 2, "type": "not_a_plc/get_program", "entry_id": entry_a.entry_id},
    )
    assert conn2.results[2]["program"]["meta"]["name"] == "WS demo"

    # An unknown entry_id errors instead of falling back.
    conn3 = FakeConnection()
    ws_get_program(
        hass,
        conn3,
        {"id": 3, "type": "not_a_plc/get_program", "entry_id": "does-not-exist"},
    )
    assert conn3.errors[3][0] == ERR_NOT_LOADED


_EDITED_PROGRAM = {
    "meta": {"name": "Edited"},
    "tags": {
        "i": {"kind": "input", "source": "binary_sensor.trigger"},
        "o": {"kind": "coil"},
        "m": {"kind": "memory"},
    },
    "networks": [
        {
            "id": "n",
            "rungs": [
                {
                    "id": "r",
                    "series": [{"type": "contact", "tag": "i"}],
                    "coils": [{"type": "coil", "tag": "m", "mode": "S"}],
                }
            ],
        }
    ],
}


async def test_save_program_replaces_the_running_program(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_program(monkeypatch, _PROGRAM)
    hass.states.async_set("binary_sensor.trigger", "off")
    entry = await _setup(hass)

    conn = FakeConnection()
    ws_save_program(
        hass,
        conn,
        {"id": 7, "type": "not_a_plc/save_program", "program": _EDITED_PROGRAM},
    )
    await conn.async_wait()
    await hass.async_block_till_done()

    assert 7 in conn.results
    # The service reloaded and re-read the new program from .storage.
    coordinator = hass.data[DOMAIN][entry.entry_id][DATA_COORDINATOR]
    assert coordinator.program.meta["name"] == "Edited"
    assert "m" in coordinator.program.memory_tags()


async def test_save_program_rejects_an_invalid_program(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_program(monkeypatch, _PROGRAM)
    hass.states.async_set("binary_sensor.trigger", "off")
    await _setup(hass)

    conn = FakeConnection()
    bad = {"tags": {"x": {"kind": "weird"}}, "networks": []}
    ws_save_program(
        hass, conn, {"id": 8, "type": "not_a_plc/save_program", "program": bad}
    )
    await conn.async_wait()

    assert conn.errors[8][0] == ERR_INVALID_PROGRAM
    assert 8 not in conn.results


async def test_get_program_text_round_trips(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_program(monkeypatch, _PROGRAM)
    hass.states.async_set("binary_sensor.trigger", "off")
    await _setup(hass)

    conn = FakeConnection()
    ws_get_program_text(hass, conn, {"id": 9, "type": "not_a_plc/get_program_text"})
    text = conn.results[9]["text"]
    again = program_from_text(text)
    assert again.to_dict()["tags"]["i"]["kind"] == "input"
