"""Config-flow tests (require pytest-homeassistant-custom-component)."""

from __future__ import annotations

from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.not_a_plc.const import DOMAIN


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
