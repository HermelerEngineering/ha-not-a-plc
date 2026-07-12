"""Config-flow tests (require pytest-homeassistant-custom-component)."""

from __future__ import annotations

from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.not_a_plc.const import (
    CONF_SCAN_INTERVAL,
    CONF_STARTER,
    DATA_COORDINATOR,
    DOMAIN,
)


async def test_user_flow_creates_named_service(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM

    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "name": "Ventilation",
            "starter_program": "daylight",
            "scan_interval_ms": "1000",
        },
    )
    await hass.async_block_till_done()

    assert result2["type"] is FlowResultType.CREATE_ENTRY
    assert result2["title"] == "Ventilation"
    assert result2["data"]["starter_program"] == "daylight"
    # The preset string is stored as an int.
    assert result2["data"]["scan_interval_ms"] == 1000


async def test_multiple_services_allowed(hass: HomeAssistant) -> None:
    for name in ("Service A", "Service B"):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "name": name,
                "starter_program": "render",
                "scan_interval_ms": "500",
            },
        )
        assert result2["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    assert len(hass.config_entries.async_entries(DOMAIN)) == 2


async def test_options_flow_rebinds_input_and_interval(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Thermo",
        data={CONF_STARTER: "thermostat", CONF_SCAN_INTERVAL: 1000},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM

    result2 = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"temp": "sensor.my_temperature", CONF_SCAN_INTERVAL: "2000"},
    )
    assert result2["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    # The service reloaded and re-read the rebound program from .storage.
    coordinator = hass.data[DOMAIN][entry.entry_id][DATA_COORDINATOR]
    assert coordinator.program.tags["temp"].source == "sensor.my_temperature"
    assert coordinator.program.scan_interval_ms == 2000
