"""The cyclic driver: reads inputs, solves the program, publishes outputs.

Each refresh performs one scan cycle:

    snapshot  -> read input entities into a frozen process image
    solve     -> evaluate(program, image, now, previous)  (pure, in the engine)
    write     -> return coil states; run write-on-change service calls

Coil/memory entities are ``CoordinatorEntity`` instances that read their value
from ``coordinator.data``.

State that must survive a restart (``retain: true`` memory bits) is persisted to
``.storage`` here — the engine stays pure and knows nothing about it. On startup
the retained values seed the previous-output image, so the first scan continues a
latch where it left off.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    DEFAULT_TRUE_STATES,
    DOMAIN,
    RETAIN_SAVE_DELAY,
    STORAGE_KEY_PREFIX,
    STORAGE_VERSION,
)
from .engine import Program, ProgramError, evaluate


class LadderCoordinator(DataUpdateCoordinator[dict[str, bool]]):
    """Runs the program on a fixed cycle and holds the latest output image."""

    def __init__(self, hass: HomeAssistant, program: Program, entry_id: str) -> None:
        super().__init__(
            hass,
            logging.getLogger(__name__),
            name=DOMAIN,
            update_interval=timedelta(milliseconds=program.scan_interval_ms),
        )
        self.program = program
        self._previous: dict[str, bool] = {}
        # Last frozen input snapshot, exposed to the websocket status view so the
        # frontend can colour input contacts as well as coils/memory bits.
        self._last_inputs: dict[str, Any] = {}
        # Last successfully-read value per input tag, for on_unavailable="hold".
        self._input_history: dict[str, Any] = {}
        # Precompute the per-tag "true state" sets (lower-cased) for BOOL inputs.
        self._true_states: dict[str, frozenset[str]] = {
            name: (
                frozenset(s.lower() for s in tag.true_states)
                if tag.true_states is not None
                else DEFAULT_TRUE_STATES
            )
            for name, tag in program.input_tags().items()
        }
        self._retained_tags = [
            name for name, tag in program.memory_tags().items() if tag.retain
        ]
        # The retained snapshot we last scheduled to persist. We only touch disk
        # when a retained bit actually changes — never once-per-scan — so a steady
        # program does no disk writes at all after startup.
        self._saved_retained: dict[str, bool] | None = None
        self._store: Store[dict[str, bool]] = Store(
            hass, STORAGE_VERSION, f"{STORAGE_KEY_PREFIX}.{entry_id}"
        )

    # --- Retention ----------------------------------------------------------

    async def async_load_retained(self) -> None:
        """Seed the previous-output image from persisted retained bits.

        Called once before the first refresh so a latch resumes after a restart.
        """
        if not self._retained_tags:
            return
        stored = await self._store.async_load()
        if not stored:
            return
        for name in self._retained_tags:
            if name in stored:
                self._previous[name] = bool(stored[name])
        # Treat the loaded values as already persisted, so an unchanged program
        # never rewrites identical state right after startup.
        self._saved_retained = self._retained_snapshot()

    def _retained_snapshot(self) -> dict[str, bool]:
        return {
            name: bool(self._previous.get(name, False)) for name in self._retained_tags
        }

    async def async_save_retained(self) -> None:
        """Flush retained bits to storage now (called on unload/shutdown)."""
        if self._retained_tags:
            snapshot = self._retained_snapshot()
            self._saved_retained = snapshot
            await self._store.async_save(snapshot)

    # --- Snapshot -----------------------------------------------------------

    def _read_input(self, tag_name: str) -> Any:
        """Read one input tag's source entity into a typed value."""
        tag = self.program.tags[tag_name]
        assert tag.source is not None  # guaranteed for input tags by the model
        state: State | None = self.hass.states.get(tag.source)

        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return self._on_unavailable(tag_name)

        if tag.type == "REAL":
            try:
                value: Any = float(state.state)
            except (ValueError, TypeError):
                return self._on_unavailable(tag_name)
        else:
            value = state.state.lower() in self._true_states[tag_name]

        self._input_history[tag_name] = value
        return value

    def _on_unavailable(self, tag_name: str) -> Any:
        """Value to use when an input is unavailable/unreadable."""
        tag = self.program.tags[tag_name]
        if tag.on_unavailable == "hold" and tag_name in self._input_history:
            return self._input_history[tag_name]
        return 0.0 if tag.type == "REAL" else False

    def _snapshot(self) -> dict[str, Any]:
        return {name: self._read_input(name) for name in self.program.input_tags()}

    # --- Solve + write ------------------------------------------------------

    async def _async_update_data(self) -> dict[str, bool]:
        try:
            image = self._snapshot()
            outputs = evaluate(
                self.program, image, now=dt_util.utcnow(), previous=self._previous
            )
        except ProgramError as err:
            raise UpdateFailed(f"program error: {err}") from err

        self._last_inputs = image

        await self._write_on_change(outputs)
        self._previous = outputs
        if self._retained_tags:
            snapshot = self._retained_snapshot()
            # Only schedule a (debounced) write when a retained bit changed.
            if snapshot != self._saved_retained:
                self._saved_retained = snapshot
                self._store.async_delay_save(self._retained_snapshot, RETAIN_SAVE_DELAY)
        return outputs

    async def _write_on_change(self, outputs: dict[str, bool]) -> None:
        """Actuate real entities for coils that changed and have a writes binding."""
        for name, tag in self.program.coil_tags().items():
            if tag.writes is None:
                continue
            new = outputs.get(name, False)
            if self._previous.get(name) == new:
                continue
            service = "turn_on" if new else "turn_off"
            domain = tag.writes.target.split(".", 1)[0]
            await self.hass.services.async_call(
                domain,
                service,
                {"entity_id": tag.writes.target},
                blocking=False,
            )

    # --- Status view --------------------------------------------------------

    def state_image(self) -> dict[str, Any]:
        """The full process image after the last scan: inputs + memory + coils.

        This is what the read-only status view subscribes to. Keys are tag names;
        values are booleans (BOOL) or floats (REAL). Returns an empty dict before
        the first scan has produced any state.
        """
        image: dict[str, Any] = dict(self._last_inputs)
        if self.data:
            image.update(self.data)
        return image
