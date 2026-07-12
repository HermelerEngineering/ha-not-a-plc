"""Config + options flow for Not-a-PLC.

Each run of the *config* flow creates one independent *service* (its own device,
program, entities and scan loop) — there is no single-instance limit. The user
names the service and picks a starter program and scan interval in the UI; the
service then owns that program in ``.storage``.

The *options* flow lets the user, still entirely in the UI, bind each program
input to a Home Assistant entity and change the scan interval. It writes the
updated program back to ``.storage`` and reloads the service. Full program
editing (adding logic) is the future graphical editor.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.storage import Store

from .const import (
    BUNDLED_PROGRAMS,
    CONF_SCAN_INTERVAL,
    CONF_STARTER,
    DATA_COORDINATOR,
    DEFAULT_SCAN_INTERVAL_MS,
    DEFAULT_STARTER,
    DOMAIN,
    SCAN_INTERVAL_PRESETS,
    STORAGE_PROGRAM_PREFIX,
    STORAGE_VERSION,
)
from .engine import Program, ProgramError


def _interval_label(ms: int) -> str:
    return f"{ms} ms" if ms < 1000 else f"{ms // 1000} s"


def _interval_selector() -> selector.SelectSelector:
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[
                selector.SelectOptionDict(value=str(ms), label=_interval_label(ms))
                for ms in SCAN_INTERVAL_PRESETS
            ],
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )


class LadderConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Not-a-PLC."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: Any) -> OptionsFlow:
        return LadderOptionsFlow()

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
                vol.Required("name", default="Not-a-PLC"): selector.TextSelector(),
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
                ): _interval_selector(),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)


class LadderOptionsFlow(OptionsFlow):
    """Bind program inputs to entities and set the scan interval, in the UI."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self.config_entry
        data = self.hass.data.get(DOMAIN, {}).get(entry.entry_id)
        coordinator = data.get(DATA_COORDINATOR) if data else None
        if coordinator is None:
            return self.async_abort(reason="not_loaded")

        program: Program = coordinator.program
        inputs = program.input_tags()

        if user_input is not None:
            new_program = program.to_dict()
            for name in inputs:
                new_program["tags"][name]["source"] = user_input[name]
            new_program["scan_interval_ms"] = int(user_input[CONF_SCAN_INTERVAL])
            try:
                Program.from_dict(new_program)
            except ProgramError:
                return self.async_show_form(
                    step_id="init",
                    data_schema=self._schema(program, user_input),
                    errors={"base": "invalid_program"},
                )
            store: Store[dict[str, Any]] = Store(
                self.hass, STORAGE_VERSION, f"{STORAGE_PROGRAM_PREFIX}.{entry.entry_id}"
            )
            await store.async_save(new_program)
            # Persisting options triggers the entry's update listener, which
            # reloads the service so it re-reads the program from .storage.
            return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(step_id="init", data_schema=self._schema(program))

    def _schema(
        self, program: Program, current: dict[str, Any] | None = None
    ) -> vol.Schema:
        current = current or {}
        fields: dict[Any, Any] = {}
        for name, tag in program.input_tags().items():
            if tag.type == "REAL":
                config = selector.EntitySelectorConfig(
                    domain=["sensor", "input_number", "number"]
                )
            else:
                config = selector.EntitySelectorConfig()
            default = current.get(name, tag.source)
            fields[vol.Required(name, default=default)] = selector.EntitySelector(
                config
            )
        fields[
            vol.Required(
                CONF_SCAN_INTERVAL,
                default=str(current.get(CONF_SCAN_INTERVAL, program.scan_interval_ms)),
            )
        ] = _interval_selector()
        return vol.Schema(fields)
