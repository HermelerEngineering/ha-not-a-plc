"""Phase-1 integration tests (require pytest-homeassistant-custom-component).

These exercise the HA layer end-to-end: the write-on-change executor, the
``on_unavailable`` policy with input history, and retention across a restart.
Each test injects a small program by monkeypatching the bundled-program loader.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
)

from custom_components import not_a_plc as integration
from custom_components.not_a_plc.const import DOMAIN
from custom_components.not_a_plc.engine import Program


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


async def test_writes_executor_actuates_on_change(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_program(
        monkeypatch,
        {
            "tags": {
                "i": {"kind": "input", "source": "binary_sensor.trigger"},
                "o": {"kind": "coil", "writes": {"target": "switch.target"}},
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
        },
    )
    turn_on = async_mock_service(hass, "switch", "turn_on")
    turn_off = async_mock_service(hass, "switch", "turn_off")

    hass.states.async_set("binary_sensor.trigger", "off")
    await _setup(hass)

    hass.states.async_set("binary_sensor.trigger", "on")
    await _tick(hass)
    assert len(turn_on) == 1
    assert turn_on[0].data["entity_id"] == "switch.target"

    # No further calls while the coil stays on (write-on-change only).
    on_count = len(turn_on)
    await _tick(hass)
    assert len(turn_on) == on_count

    hass.states.async_set("binary_sensor.trigger", "off")
    await _tick(hass)
    assert turn_off[-1].data["entity_id"] == "switch.target"


async def test_on_unavailable_hold_vs_false(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_program(
        monkeypatch,
        {
            "tags": {
                "a": {
                    "kind": "input",
                    "source": "binary_sensor.a",
                    "on_unavailable": "hold",
                },
                "b": {
                    "kind": "input",
                    "source": "binary_sensor.b",
                    "on_unavailable": "false",
                },
                "oa": {"kind": "coil"},
                "ob": {"kind": "coil"},
            },
            "networks": [
                {
                    "id": "n",
                    "rungs": [
                        {
                            "id": "ra",
                            "series": [{"type": "contact", "tag": "a"}],
                            "coils": [{"type": "coil", "tag": "oa"}],
                        },
                        {
                            "id": "rb",
                            "series": [{"type": "contact", "tag": "b"}],
                            "coils": [{"type": "coil", "tag": "ob"}],
                        },
                    ],
                }
            ],
        },
    )
    hass.states.async_set("binary_sensor.a", "on")
    hass.states.async_set("binary_sensor.b", "on")
    await _setup(hass)
    await _tick(hass)
    assert hass.states.get("binary_sensor.not_a_plc_oa").state == "on"
    assert hass.states.get("binary_sensor.not_a_plc_ob").state == "on"

    # Both sources go unavailable: "hold" keeps the last good value, "false" drops.
    hass.states.async_set("binary_sensor.a", STATE_UNAVAILABLE)
    hass.states.async_set("binary_sensor.b", STATE_UNAVAILABLE)
    await _tick(hass)
    assert hass.states.get("binary_sensor.not_a_plc_oa").state == "on"
    assert hass.states.get("binary_sensor.not_a_plc_ob").state == "off"


def _latch_program() -> dict:
    return {
        "tags": {
            "set": {"kind": "input", "source": "binary_sensor.set"},
            "rst": {"kind": "input", "source": "binary_sensor.rst"},
            "m": {"kind": "memory", "retain": True},
            "out": {"kind": "coil"},
        },
        "networks": [
            {
                "id": "n",
                "rungs": [
                    {
                        "id": "r1",
                        "series": [{"type": "contact", "tag": "set"}],
                        "coils": [{"type": "coil", "tag": "m", "mode": "S"}],
                    },
                    {
                        "id": "r2",
                        "series": [{"type": "contact", "tag": "rst"}],
                        "coils": [{"type": "coil", "tag": "m", "mode": "R"}],
                    },
                    {
                        "id": "r3",
                        "series": [{"type": "contact", "tag": "m"}],
                        "coils": [{"type": "coil", "tag": "out"}],
                    },
                ],
            }
        ],
    }


async def test_retained_latch_survives_reload(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_program(monkeypatch, _latch_program())
    hass.states.async_set("binary_sensor.set", "off")
    hass.states.async_set("binary_sensor.rst", "off")
    entry = await _setup(hass)

    # Latch on, then stop driving it: it holds (retentive across scans).
    hass.states.async_set("binary_sensor.set", "on")
    await _tick(hass)
    assert hass.states.get("binary_sensor.not_a_plc_out").state == "on"
    hass.states.async_set("binary_sensor.set", "off")
    await _tick(hass)
    assert hass.states.get("binary_sensor.not_a_plc_out").state == "on"

    # Reload the entry (a "restart"). Both inputs are off, so only persisted
    # retention can keep the coil on.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.not_a_plc_out").state == "on"

    # Reset still clears it after the restart.
    hass.states.async_set("binary_sensor.rst", "on")
    await _tick(hass)
    assert hass.states.get("binary_sensor.not_a_plc_out").state == "off"
