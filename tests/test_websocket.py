"""Phase-2 websocket tests (require pytest-homeassistant-custom-component).

Exercise the read-only status-view API end-to-end: ``get_program`` returns the
IR, and ``subscribe_state`` pushes the process image immediately and again after
each scan.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.typing import WebSocketGenerator

from custom_components import not_a_plc as integration
from custom_components.not_a_plc.const import DOMAIN
from custom_components.not_a_plc.engine import Program

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
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_program(monkeypatch, _PROGRAM)
    hass.states.async_set("binary_sensor.trigger", "off")
    await _setup(hass)

    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "not_a_plc/get_program"})
    msg = await client.receive_json()

    assert msg["success"]
    program = msg["result"]["program"]
    assert program["meta"]["name"] == "WS demo"
    assert program["tags"]["i"]["kind"] == "input"
    assert program["tags"]["i"]["source"] == "binary_sensor.trigger"
    assert program["networks"][0]["rungs"][0]["coils"][0]["tag"] == "o"


async def test_subscribe_state_pushes_initial_and_on_scan(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_program(monkeypatch, _PROGRAM)
    hass.states.async_set("binary_sensor.trigger", "off")
    await _setup(hass)

    client = await hass_ws_client(hass)
    await client.send_json({"id": 5, "type": "not_a_plc/subscribe_state"})

    # First the subscribe acknowledgement, then the immediate current image.
    ack = await client.receive_json()
    assert ack["success"]

    initial = await client.receive_json()
    assert initial["type"] == "event"
    assert initial["event"]["state"] == {"i": False, "o": False}

    # Drive the input on and advance one scan: a fresh image is pushed.
    hass.states.async_set("binary_sensor.trigger", "on")
    await _tick(hass)

    update = await client.receive_json()
    assert update["type"] == "event"
    assert update["event"]["state"] == {"i": True, "o": True}
