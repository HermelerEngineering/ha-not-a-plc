"""Config flow for Not a PLC.

Each run of this flow creates one independent *service* (its own device, program,
entities and scan loop) — there is no single-instance limit. The user names the
service and picks a starter program and scan interval entirely in the UI; the
service then owns that program in ``.storage`` (see ``__init__._async_load_program``).
Editing the program comes later with the graphical editor.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers import selector

from .const import (
    BUNDLED_PROGRAMS,
    CONF_SCAN_INTERVAL,
    CONF_STARTER,
    DEFAULT_SCAN_INTERVAL_MS,
    DEFAULT_STARTER,
    DOMAIN,
    SCAN_INTERVAL_PRESETS,
)


def _interval_label(ms: int) -> str:
    return f"{ms} ms" if ms < 1000 else f"{ms // 1000} s"


class LadderConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Not a PLC."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create a new service: name + starter program + scan interval."""
        if user_input is not None:
            return self.async_create_entry(
                title=user_input["name"],
                data={
                    CONF_STARTER: user_input[CONF_STARTER],
                    CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
                },
            )

        schema = vol.Schema(
            {
                vol.Required("name", default="Not a PLC"): selector.TextSelector(),
                vol.Required(
                    CONF_STARTER, default=DEFAULT_STARTER
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(value=pid, label=label)
                            for pid, (label, _file) in BUNDLED_PROGRAMS.items()
                        ],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required(
                    CONF_SCAN_INTERVAL, default=str(DEFAULT_SCAN_INTERVAL_MS)
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(
                                value=str(ms), label=_interval_label(ms)
                            )
                            for ms in SCAN_INTERVAL_PRESETS
                        ],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)
