"""Config flow for Not a PLC.

Phase 0 is a single-instance, no-options setup: adding the integration creates
one entry that loads the bundled demo program. Program selection/editing arrives
with the DSL importer and the graphical editor in later phases.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import DOMAIN


class LadderConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Not a PLC."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        if user_input is not None:
            return self.async_create_entry(title="Not a PLC", data={})

        return self.async_show_form(step_id="user")
