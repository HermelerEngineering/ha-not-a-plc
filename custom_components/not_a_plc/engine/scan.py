"""Program evaluation — the pure heart of the scan cycle.

``evaluate`` is a pure function: given a program, a frozen process image, the
current time, and the previous output image, it returns the new output values.
It performs no I/O and does not read the wall clock itself (``now`` is injected)
so timers become deterministic to test in later phases.

Retentive outputs (``S`` / ``R`` coils, and any coil/memory bit not written on a
given scan) carry their value across scans via the ``previous`` argument, so the
engine stays a pure function of its inputs — no hidden state. Persisting those
bits across a *restart* is the HA layer's job (see the coordinator's store).

Scope: contacts (NO/NC), series (AND), parallel branch (OR), an inline ``NOT``
power inverter, coils ``=`` / ``S`` / ``R``, ``REAL`` comparators, and stateful
function-block instances (``R_TRIG`` / ``F_TRIG``; timers/counters build on the
same ``fbs`` state threading).
"""

from __future__ import annotations

import operator
from collections.abc import Callable
from datetime import datetime
from typing import Any

from .errors import ProgramError
from .model import (
    COUNTER_TYPES,
    IMPLEMENTED_COIL_MODES,
    LATCH_TYPES,
    TIMER_TYPES,
    Branch,
    Compare,
    Contact,
    Element,
    FbRef,
    FunctionBlock,
    Not,
    Program,
)

_COMPARATORS: dict[str, Callable[[float, float], bool]] = {
    "GT": operator.gt,
    "GE": operator.ge,
    "LT": operator.lt,
    "LE": operator.le,
    "EQ": operator.eq,
    "NE": operator.ne,
}


class ScanResult(dict[str, bool]):
    """The output image after a scan, plus function-block state on ``.fbs``.

    It *is* the coil/memory output image (so ``result[tag]`` indexing and dict
    comparisons keep working), and additionally carries each function-block
    instance's state in ``.fbs`` — which the caller holds in RAM and passes back
    in on the next scan via the ``fbs`` argument, keeping ``evaluate`` pure.
    """

    fbs: dict[str, dict[str, Any]]

    def __init__(
        self, outputs: dict[str, bool], fbs: dict[str, dict[str, Any]]
    ) -> None:
        super().__init__(outputs)
        self.fbs = fbs


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


def _as_number(value: Any) -> float | None:
    """Coerce a process-image value to float, or None if it is not numeric."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _eval_compare(cmp: Compare, image: dict[str, Any]) -> bool:
    """``left <op> right``. A missing/non-numeric operand does not conduct."""
    left = _as_number(image.get(cmp.left))
    if isinstance(cmp.right, str):
        right = _as_number(image.get(cmp.right))
    else:
        right = float(cmp.right)
    if left is None or right is None:
        return False
    return _COMPARATORS[cmp.op](left, right)


def _eval_element(element: Element, image: dict[str, Any]) -> bool:
    """Evaluate a *stateless gate* element (contact / compare / branch).

    ``Not`` (an inline power inverter) and ``FbRef`` are handled by the series
    fold, not here, so they never reach this function.
    """
    if isinstance(element, Contact):
        state = _truthy(image.get(element.tag, False))
        return (not state) if element.mode == "NC" else state
    if isinstance(element, Compare):
        return _eval_compare(element, image)
    if isinstance(element, Branch):
        return any(_eval_series(path, image) for path in element.paths)
    raise ProgramError("this element cannot be evaluated as a stateless gate")


def _eval_series(elements: list[Element], image: dict[str, Any]) -> bool:
    """Left-to-right power fold of a series chain (AND).

    A ``Not`` inverts the accumulated power at its position (so ``( a OR b ) NOT``
    conducts NOR). An empty chain conducts (True), matching an empty rung.
    """
    power = True
    for el in elements:
        power = not power if isinstance(el, Not) else power and _eval_element(el, image)
    return power


def _solve_fb(
    name: str,
    block: FunctionBlock,
    clk: bool,
    now: datetime | None,
    values: dict[str, Any],
    fb_prev: dict[str, dict[str, Any]],
    fb_new: dict[str, dict[str, Any]],
) -> bool:
    """Compute a function block's output Q from its inputs and previous state.

    ``clk`` is the rung power reaching the block (the primary input CLK / IN / CU /
    CD / S). Multi-input blocks read their secondary inputs (``reset`` / ``load``)
    as tag references from ``values``. Records the new instance state in ``fb_new``
    and returns Q (which becomes the rung power leaving the block).
    """
    state = fb_prev.get(name, {})
    btype = block.type

    if btype == "R_TRIG":
        q = clk and not bool(state.get("clk", False))
        fb_new[name] = {"clk": clk, "q": q}
        return q
    if btype == "F_TRIG":
        q = (not clk) and bool(state.get("clk", False))
        fb_new[name] = {"clk": clk, "q": q}
        return q
    if btype in TIMER_TYPES:
        return _solve_timer(name, block, clk, now, state, fb_new)
    if btype in COUNTER_TYPES:
        return _solve_counter(name, block, clk, values, state, fb_new)
    if btype in LATCH_TYPES:
        return _solve_latch(name, block, clk, values, state, fb_new)
    raise ProgramError(f"function-block type '{btype}' is not implemented")


def _solve_timer(
    name: str,
    block: FunctionBlock,
    clk: bool,
    now: datetime | None,
    state: dict[str, Any],
    fb_new: dict[str, dict[str, Any]],
) -> bool:
    # Timers accumulate wall-clock time via the injected ``now`` (never scan
    # counts), so they stay deterministic under a fake clock in tests.
    if now is None:
        raise ProgramError(f"function block '{name}' ({block.type}) needs a clock")
    now_ms = now.timestamp() * 1000.0
    dt = max(0.0, now_ms - float(state.get("last_ms", now_ms)))
    preset = float(block.params["preset_ms"])
    et = float(state.get("et", 0.0))

    if block.type == "TON":
        # On-delay: Q goes true once IN has been true for the preset.
        if clk:
            et = min(preset, et + dt)
            q = et >= preset
        else:
            et = 0.0
            q = False
    elif block.type == "TOF":
        # Off-delay: Q follows IN up immediately, and holds for the preset after
        # IN drops (run-on).
        if clk:
            q = True
            et = 0.0
        elif bool(state.get("q", False)):
            et = et + dt
            q = et < preset
        else:
            q = False
            et = preset
    else:  # TP — pulse: a rising edge of IN gives Q true for exactly the preset.
        q = bool(state.get("q", False))
        if not q and clk and not bool(state.get("clk", False)):
            q = True
            et = 0.0
        if q:
            et = et + dt
            if et >= preset:
                q = False
                et = preset

    fb_new[name] = {"clk": clk, "q": q, "et": et, "last_ms": now_ms}
    return q


def _solve_counter(
    name: str,
    block: FunctionBlock,
    clk: bool,
    values: dict[str, Any],
    state: dict[str, Any],
    fb_new: dict[str, dict[str, Any]],
) -> bool:
    # Count on the rising edge of the primary input (CU / CD). CV is exposed as a
    # numeric output for comparators (see _solve_rung).
    prev_clk = bool(state.get("clk", False))
    rising = clk and not prev_clk
    cv = int(state.get("cv", 0))
    pv = int(block.params["pv"])

    if block.type == "CTU":
        reset_tag = block.params.get("reset")
        reset = _truthy(values.get(reset_tag)) if isinstance(reset_tag, str) else False
        if reset:
            cv = 0
        elif rising:
            cv = min(pv, cv + 1)
        q = cv >= pv
    else:  # CTD
        load_tag = block.params.get("load")
        load = _truthy(values.get(load_tag)) if isinstance(load_tag, str) else False
        if load:
            cv = pv
        elif rising:
            cv = max(0, cv - 1)
        q = cv <= 0

    fb_new[name] = {"clk": clk, "q": q, "cv": cv}
    return q


def _solve_latch(
    name: str,
    block: FunctionBlock,
    clk: bool,
    values: dict[str, Any],
    state: dict[str, Any],
    fb_new: dict[str, dict[str, Any]],
) -> bool:
    # S = rung power; R = the declared reset tag. SR is set-dominant, RS reset.
    reset = _truthy(values.get(block.params["reset"]))
    prev_q = bool(state.get("q", False))
    if block.type == "SR":
        q = clk or (prev_q and not reset)
    else:  # RS
        q = (clk or prev_q) and not reset
    fb_new[name] = {"q": q}
    return q


def _solve_rung(
    elements: list[Element],
    values: dict[str, Any],
    now: datetime | None,
    program: Program,
    fb_prev: dict[str, dict[str, Any]],
    fb_new: dict[str, dict[str, Any]],
) -> bool:
    """Left-to-right power solve of a rung's top-level series.

    Stateless elements gate the running power (AND); a function-block reference
    takes the running power as its input and replaces it with its output Q.
    """
    power = True
    for el in elements:
        if isinstance(el, FbRef):
            power = _solve_fb(
                el.instance,
                program.fbs[el.instance],
                power,
                now,
                values,
                fb_prev,
                fb_new,
            )
            # Surface the block's numeric outputs (timer ET, counter CV) so a later
            # compare in this scan can reference ``instance.ET`` / ``instance.CV``.
            new_state = fb_new[el.instance]
            if "et" in new_state:
                values[f"{el.instance}.ET"] = new_state["et"]
            if "cv" in new_state:
                values[f"{el.instance}.CV"] = new_state["cv"]
        elif isinstance(el, Not):
            power = not power  # inline inverter: flip the running rung power
        else:
            power = power and _eval_element(el, values)
    return power


def evaluate(
    program: Program,
    image: dict[str, Any],
    now: datetime | None = None,
    previous: dict[str, bool] | None = None,
    fbs: dict[str, dict[str, Any]] | None = None,
) -> ScanResult:
    """Solve every network top-down and return the resulting outputs.

    ``previous`` is the output image from the last scan; retentive outputs start
    from it. ``fbs`` is the function-block state from the last scan; each block
    starts from it. On the first scan (or when omitted) outputs start ``False``
    and blocks start empty.

    Returns a :class:`ScanResult` (a coil/memory tag -> bool mapping with the new
    function-block state on ``.fbs``). If two rungs write the same tag, the last
    one wins (deterministic, matching a real scan order): an ``R`` after an ``S``
    on the same tag makes reset dominant, and vice versa.
    """
    # Coil and memory bits are retentive: they start from the previous scan (so
    # S/R latches and unwritten coils carry). Temp bits are scratch — they always
    # start False each scan, never carrying across, and are never persisted.
    outputs: dict[str, bool] = {name: False for name in program.coil_tags()}
    outputs.update({name: False for name in program.memory_tags()})
    if previous:
        for name in list(outputs):
            if name in previous:
                outputs[name] = bool(previous[name])
    outputs.update({name: False for name in program.temp_tags()})

    # Contacts solve against inputs *and* the coil/memory bits computed so far.
    # Bits set by an earlier rung are visible to a later one within the same scan
    # (top-down order), which is what makes rung ordering meaningful.
    values: dict[str, Any] = {**image, **outputs}

    fb_prev = fbs or {}
    fb_new: dict[str, dict[str, Any]] = {}

    for network in program.networks:
        for rung in network.rungs:
            energised = _solve_rung(rung.series, values, now, program, fb_prev, fb_new)
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

    return ScanResult(outputs, fb_new)
