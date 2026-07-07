"""Program evaluation — the pure heart of the scan cycle.

``evaluate`` is a pure function: given a program, a frozen process image, the
current time, and the previous output image, it returns the new output values.
It performs no I/O and does not read the wall clock itself (``now`` is injected)
so timers become deterministic to test in later phases.

Retentive outputs (``S`` / ``R`` coils, and any coil/memory bit not written on a
given scan) carry their value across scans via the ``previous`` argument, so the
engine stays a pure function of its inputs — no hidden state. Persisting those
bits across a *restart* is the HA layer's job (see the coordinator's store).

Phase 1 scope: contacts (NO/NC), series (AND), parallel branch (OR), ``NOT``
groups, and coils ``=`` / ``S`` / ``R``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .errors import ProgramError
from .model import (
    IMPLEMENTED_COIL_MODES,
    Contact,
    Element,
    Not,
    Program,
)


def _truthy(value: Any) -> bool:
    """Interpret a process-image value as a boolean.

    The process image is built by the caller (in HA, the coordinator maps entity
    states to bool/REAL). Here we only coerce the already-typed value.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    return bool(value)


def _eval_element(element: Element, image: dict[str, Any]) -> bool:
    if isinstance(element, Contact):
        state = _truthy(image.get(element.tag, False))
        return (not state) if element.mode == "NC" else state
    if isinstance(element, Not):
        return not _eval_series(element.inner, image)
    # Branch: OR of its series paths.
    return any(_eval_series(path, image) for path in element.paths)


def _eval_series(elements: list[Element], image: dict[str, Any]) -> bool:
    """AND of every position in a series chain. Empty chain conducts (True)."""
    return all(_eval_element(el, image) for el in elements)


def evaluate(
    program: Program,
    image: dict[str, Any],
    now: datetime | None = None,
    previous: dict[str, bool] | None = None,
) -> dict[str, bool]:
    """Solve every network top-down and return the resulting output values.

    ``previous`` is the output image from the last scan; retentive outputs start
    from it. On the first scan (or when omitted) every output starts ``False``.

    Returns a mapping of coil/memory tag name -> bool. If two rungs write the
    same tag, the last one wins (deterministic, matching a real scan order): an
    ``R`` after an ``S`` on the same tag makes reset dominant, and vice versa.
    """
    outputs: dict[str, bool] = {name: False for name in program.coil_tags()}
    outputs.update({name: False for name in program.memory_tags()})
    if previous:
        for name in outputs:
            if name in previous:
                outputs[name] = bool(previous[name])

    # Contacts solve against inputs *and* the coil/memory bits computed so far.
    # Bits set by an earlier rung are visible to a later one within the same scan
    # (top-down order), which is what makes rung ordering meaningful.
    values: dict[str, Any] = {**image, **outputs}

    for network in program.networks:
        for rung in network.rungs:
            energised = _eval_series(rung.series, values)
            for coil in rung.coils:
                if coil.mode not in IMPLEMENTED_COIL_MODES:
                    raise ProgramError(
                        f"coil mode '{coil.mode}' is not implemented in this phase "
                        f"(tag '{coil.tag}')"
                    )
                if coil.mode == "=":
                    new = energised
                elif coil.mode == "S":
                    new = True if energised else outputs[coil.tag]
                else:  # "R"
                    new = False if energised else outputs[coil.tag]
                outputs[coil.tag] = new
                values[coil.tag] = new

    return outputs
