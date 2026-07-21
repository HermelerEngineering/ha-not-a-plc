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
    SOURCE_TYPES,
    TIMER_TYPES,
    Action,
    Branch,
    Calc,
    Compare,
    Contact,
    Element,
    FbRef,
    FunctionBlock,
    Move,
    Not,
    Program,
)

# An output-image value: a coil/memory bit (bool) or a REAL move target (float).
OutputValue = bool | float

_COMPARATORS: dict[str, Callable[[float, float], bool]] = {
    "GT": operator.gt,
    "GE": operator.ge,
    "LT": operator.lt,
    "LE": operator.le,
    "EQ": operator.eq,
    "NE": operator.ne,
}


class ScanResult(dict[str, OutputValue]):
    """The output image after a scan, plus function-block state on ``.fbs``.

    It *is* the output image — coil/memory bits (bool) and REAL move targets
    (float) — so ``result[tag]`` indexing and dict comparisons keep working, and
    additionally carries each function-block instance's state in ``.fbs`` — which
    the caller holds in RAM and passes back in on the next scan via the ``fbs``
    argument, keeping ``evaluate`` pure.

    ``.actions`` maps a rung key (``"<network id>/<rung id>"``) to whether that
    rung — which has at least one service-call (``Action``) output — is energised
    this scan. The coordinator compares against the previous scan to fire each
    action's service on the rising edge (a level here, edge detection in the HA
    layer, so ``evaluate`` stays pure).
    """

    fbs: dict[str, dict[str, Any]]
    actions: dict[str, bool]

    def __init__(
        self,
        outputs: dict[str, OutputValue],
        fbs: dict[str, dict[str, Any]],
        actions: dict[str, bool] | None = None,
    ) -> None:
        super().__init__(outputs)
        self.fbs = fbs
        self.actions = actions or {}


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


def _resolve_operand(
    operand: float | int | str, values: dict[str, Any]
) -> float | None:
    """Resolve a move/calc operand to a float, or None if missing/non-numeric."""
    if isinstance(operand, str):
        return _as_number(values.get(operand))
    return float(operand)


def _apply_calc(op: str, a: float, b: float) -> float | None:
    if op == "ADD":
        return a + b
    if op == "SUB":
        return a - b
    if op == "MUL":
        return a * b
    return None if b == 0 else a / b  # DIV; guard divide-by-zero


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
    # S = rung power; R = the declared reset tag. Following the common PLC (Siemens)
    # convention where the *last* input in the name dominates: SR is reset-dominant,
    # RS is set-dominant (so when S and R are both true, SR -> off, RS -> on).
    reset = _truthy(values.get(block.params["reset"]))
    prev_q = bool(state.get("q", False))
    if block.type == "SR":
        q = (clk or prev_q) and not reset  # reset dominant
    else:  # RS
        q = clk or (prev_q and not reset)  # set dominant
    fb_new[name] = {"q": q}
    return q


def _clock_fields(now: datetime) -> dict[str, float]:
    """The local time/date fields a CLOCK block exposes, as REAL values.

    ``now`` is whatever the caller injected; the HA layer passes local time, so
    these read as wall-clock values. ``TOD`` is minutes since midnight (0-1439),
    which turns a window spanning midnight into a single comparison. ``WD`` is the
    ISO weekday: 1 = Monday .. 7 = Sunday.
    """
    return {
        "H": float(now.hour),
        "M": float(now.minute),
        "S": float(now.second),
        "TOD": float(now.hour * 60 + now.minute),
        "WD": float(now.isoweekday()),
        "D": float(now.day),
        "MO": float(now.month),
        "Y": float(now.year),
    }


def _solve_sources(
    program: Program,
    values: dict[str, Any],
    now: datetime | None,
    fb_new: dict[str, dict[str, Any]],
) -> None:
    """Solve the source blocks (CLOCK) once, before any rung.

    A source block has no rung input and no state, so it is not placed as an ``fb``
    element: declaring the instance is enough. Its outputs are injected into
    ``values`` up front, so every rung in the scan sees the same, frozen reading —
    the time cannot change midway through a scan.
    """
    for name, block in program.fbs.items():
        if block.type not in SOURCE_TYPES:
            continue
        if now is None:
            raise ProgramError(f"function block '{name}' ({block.type}) needs a clock")
        fields = _clock_fields(now)
        for out, value in fields.items():
            values[f"{name}.{out}"] = value
        # Mirror into the block state so the coordinator can publish it (and the
        # card colour it) the same way it does for timer ET / counter CV.
        fb_new[name] = {out.lower(): value for out, value in fields.items()}


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
    previous: dict[str, OutputValue] | None = None,
    fbs: dict[str, dict[str, Any]] | None = None,
) -> ScanResult:
    """Solve every network top-down and return the resulting outputs.

    ``previous`` is the output image from the last scan; retentive outputs start
    from it. ``fbs`` is the function-block state from the last scan; each block
    starts from it. On the first scan (or when omitted) BOOL outputs start
    ``False``, REAL outputs start ``0.0`` and blocks start empty.

    Returns a :class:`ScanResult` (a coil/memory/move tag -> bool|float mapping
    with the new function-block state on ``.fbs``). If two rungs write the same
    tag, the last one wins (deterministic, matching a real scan order): an ``R``
    after an ``S`` on the same tag makes reset dominant, and vice versa.
    """

    def seed(tag: Any) -> OutputValue:
        return 0.0 if tag.type == "REAL" else False

    # Coil and memory outputs are retentive: they start from the previous scan (so
    # S/R latches, unwritten coils, and moves that did not fire carry). Temp
    # outputs are scratch — they always reset each scan and are never persisted.
    outputs: dict[str, OutputValue] = {
        name: seed(tag) for name, tag in program.coil_tags().items()
    }
    outputs.update({name: seed(tag) for name, tag in program.memory_tags().items()})
    if previous:
        for name in list(outputs):
            if name in previous:
                outputs[name] = previous[name]
    outputs.update({name: seed(tag) for name, tag in program.temp_tags().items()})

    # Contacts solve against inputs *and* the outputs computed so far. Bits set by
    # an earlier rung are visible to a later one within the same scan (top-down
    # order), which is what makes rung ordering meaningful.
    values: dict[str, Any] = {**image, **outputs}

    fb_prev = fbs or {}
    fb_new: dict[str, dict[str, Any]] = {}
    # Source blocks (CLOCK) are read once up front, so the whole scan sees one
    # frozen reading and their outputs work without placing an `fb` element.
    _solve_sources(program, values, now, fb_new)
    # Per-rung energised level for rungs with a service-call output; the coordinator
    # turns this into a rising-edge fire (see ScanResult.actions).
    actions: dict[str, bool] = {}

    for network in program.networks:
        for rung in network.rungs:
            energised = _solve_rung(rung.series, values, now, program, fb_prev, fb_new)
            if any(isinstance(c, Action) for c in rung.coils):
                actions[f"{network.id}/{rung.id}"] = energised
            for output in rung.coils:
                if isinstance(output, Action):
                    continue  # a side effect fired by the coordinator, not a value
                if isinstance(output, Move):
                    # Copy the REAL source into the destination when energised;
                    # otherwise leave the destination at its previous value.
                    if energised:
                        src = _resolve_operand(output.src, values)
                        if src is not None:
                            outputs[output.dst] = src
                    values[output.dst] = outputs[output.dst]
                    continue
                if isinstance(output, Calc):
                    # dst := a <op> b when energised; a missing operand or a
                    # divide-by-zero leaves the destination unchanged.
                    if energised:
                        a = _resolve_operand(output.a, values)
                        b = _resolve_operand(output.b, values)
                        if a is not None and b is not None:
                            result = _apply_calc(output.op, a, b)
                            if result is not None:
                                outputs[output.dst] = result
                    values[output.dst] = outputs[output.dst]
                    continue
                if output.mode not in IMPLEMENTED_COIL_MODES:
                    raise ProgramError(
                        f"coil mode '{output.mode}' is not implemented in this phase "
                        f"(tag '{output.tag}')"
                    )
                new: OutputValue
                if output.mode == "=":
                    new = energised
                elif output.mode == "S":
                    new = True if energised else outputs[output.tag]
                else:  # "R"
                    new = False if energised else outputs[output.tag]
                outputs[output.tag] = new
                values[output.tag] = new

    return ScanResult(outputs, fb_new, actions)
