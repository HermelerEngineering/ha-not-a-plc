"""End-to-end smoke test: setup, then the coil follows its input via the tick."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.not_a_plc.const import DOMAIN

COIL_ENTITY = "binary_sensor.not_a_plc_daylight"


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_coil_follows_input(hass: HomeAssistant) -> None:
    hass.states.async_set("sun.sun", "below_horizon")
    await _setup(hass)

    state = hass.states.get(COIL_ENTITY)
    assert state is not None
    assert state.state == "off"

    # Flip the input and advance the clock past one scan interval.
    hass.states.async_set("sun.sun", "above_horizon")
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1))
    await hass.async_block_till_done()

    assert hass.states.get(COIL_ENTITY).state == "on"


async def test_unload(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert DOMAIN not in hass.data or entry.entry_id not in hass.data[DOMAIN]
