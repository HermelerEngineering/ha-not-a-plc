"""In-memory model (IR) for a ladder program.

This module is the single source of truth for the program structure. Both the
text/YAML DSL and (later) the graphical editor serialise to and from the exact
dict shape handled here by :meth:`Program.from_dict` / :meth:`Program.to_dict`.

Design rules (do not break without updating docs/project-plan.md):
- Pure Python, standard library only. No Home Assistant imports.
- The canonical binding of a tag is a bare ``entity_id`` string; friendly and
  device names are resolved live by the frontend, never stored here.
- A rung is a *series chain*; each position is a single element (contact) or a
  *parallel branch* (list of sub-chains = OR). Chains end in one or more coils.

Supports contacts (NO/NC), series/parallel, an inline ``NOT`` power inverter,
``REAL`` comparators, function-block instances (``fbs`` + inline ``FbRef``), and
rung outputs: boolean coils ``=`` / ``S`` / ``R`` and REAL ``move`` / ``calc``
(``dst := src`` and ``dst := a <op> b``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from .errors import ProgramError

# --- Type aliases -----------------------------------------------------------

TagKind = Literal["input", "coil", "memory", "temp"]
TagType = Literal["BOOL", "REAL", "TIME"]
ContactMode = Literal["NO", "NC"]
CoilMode = Literal["=", "S", "R"]
CompareOp = Literal["GT", "GE", "LT", "LE", "EQ", "NE"]
CalcOp = Literal["ADD", "SUB", "MUL", "DIV"]
UnavailablePolicy = Literal["false", "hold"]

# Arithmetic operators the evaluator implements for a ``calc`` output.
CALC_OPS: frozenset[str] = frozenset({"ADD", "SUB", "MUL", "DIV"})

# Coil modes that the evaluator actually implements.
IMPLEMENTED_COIL_MODES: frozenset[str] = frozenset({"=", "S", "R"})

# Comparison operators the evaluator implements (phase 3).
COMPARE_OPS: frozenset[str] = frozenset({"GT", "GE", "LT", "LE", "EQ", "NE"})

# Function-block types the engine implements (phase 3, growing).
TIMER_TYPES: frozenset[str] = frozenset({"TON", "TOF", "TP"})
COUNTER_TYPES: frozenset[str] = frozenset({"CTU", "CTD"})
LATCH_TYPES: frozenset[str] = frozenset({"SR", "RS"})
KNOWN_FB_TYPES: frozenset[str] = (
    frozenset({"R_TRIG", "F_TRIG"}) | TIMER_TYPES | COUNTER_TYPES | LATCH_TYPES
)


def _fb_numeric_outputs(fb_type: str) -> frozenset[str]:
    """The REAL outputs a function block exposes for use in a comparator.

    Referenced as ``instance.<NAME>`` (e.g. ``t1.ET`` / ``c1.CV``). Timers expose
    the elapsed time ``ET``; counters expose their count ``CV``.
    """
    if fb_type in TIMER_TYPES:
        return frozenset({"ET"})
    if fb_type in COUNTER_TYPES:
        return frozenset({"CV"})
    return frozenset()


def _fb_referenced_tags(fb: FunctionBlock) -> list[str]:
    """Tag names a block's declaration references as secondary inputs.

    Multi-input blocks (counters, latches) name their extra boolean inputs
    (``reset`` / ``load``) in the declaration; the primary input is the rung power.
    """
    return [
        value
        for param in ("reset", "load")
        if isinstance((value := fb.params.get(param)), str)
    ]


# --- Helpers ----------------------------------------------------------------


def _require(condition: object, message: str) -> None:
    if not condition:
        raise ProgramError(message)


def _get(mapping: dict[str, Any], key: str, where: str) -> Any:
    _require(isinstance(mapping, dict), f"{where}: expected an object")
    _require(key in mapping, f"{where}: missing required key '{key}'")
    return mapping[key]


# --- Tags -------------------------------------------------------------------


@dataclass(slots=True)
class WritesBinding:
    """Executor binding: on write-on-change, actuate a real HA entity.

    A BOOL coil actuates with ``turn_on`` / ``turn_off`` on the target's domain
    (``service`` / ``value_key`` are ``None``). A REAL coil writes its value via an
    explicit ``service`` (``domain.service``, e.g. ``light.turn_on``) carrying the
    value under ``value_key`` (e.g. ``brightness_pct``). The engine only stores the
    binding; the coordinator performs the call.
    """

    target: str  # entity_id
    service: str | None = None  # "domain.service" for REAL writes
    value_key: str | None = None  # service-data key carrying the REAL value

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"target": self.target}
        if self.service is not None:
            data["service"] = self.service
        if self.value_key is not None:
            data["value_key"] = self.value_key
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any], where: str) -> WritesBinding:
        target = _get(data, "target", where)
        _require(
            isinstance(target, str) and target,
            f"{where}: 'target' must be a non-empty entity_id",
        )
        service = data.get("service")
        if service is not None:
            _require(
                isinstance(service, str) and "." in service,
                f"{where}: 'service' must be 'domain.service'",
            )
        value_key = data.get("value_key")
        if value_key is not None:
            _require(
                isinstance(value_key, str) and value_key,
                f"{where}: 'value_key' must be a non-empty string",
            )
        # A REAL write needs both service and value_key (or neither, for BOOL).
        _require(
            (service is None) == (value_key is None),
            f"{where}: a REAL write needs both 'service' and 'value_key'",
        )
        return cls(target=target, service=service, value_key=value_key)


@dataclass(slots=True)
class Tag:
    name: str
    kind: TagKind
    type: TagType = "BOOL"
    source: str | None = None  # entity_id, for kind == "input"
    writes: WritesBinding | None = None  # for kind == "coil"
    retain: bool = False  # for kind == "memory"
    on_unavailable: UnavailablePolicy = "false"
    # For BOOL input tags: the entity states that read as True. ``None`` means
    # "use the coordinator's default mapping". Interpretation happens in the HA
    # layer; the engine never sees raw entity strings.
    true_states: tuple[str, ...] | None = None
    # For input tags: read ``state.attributes[attribute]`` instead of the entity
    # state (e.g. a light's ``brightness``). ``None`` reads the state as usual. The
    # engine never sees this — the coordinator resolves it to the same typed value.
    attribute: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"kind": self.kind, "type": self.type}
        if self.source is not None:
            data["source"] = self.source
        if self.writes is not None:
            data["writes"] = self.writes.to_dict()
        if self.kind == "memory":
            data["retain"] = self.retain
        if self.kind == "input":
            data["on_unavailable"] = self.on_unavailable
            if self.true_states is not None:
                data["true_states"] = list(self.true_states)
            if self.attribute is not None:
                data["attribute"] = self.attribute
        return data

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> Tag:
        where = f"tag '{name}'"
        kind = _get(data, "kind", where)
        _require(
            kind in ("input", "coil", "memory", "temp"),
            f"{where}: invalid kind '{kind}'",
        )

        type_ = data.get("type", "BOOL")
        _require(type_ in ("BOOL", "REAL", "TIME"), f"{where}: invalid type '{type_}'")

        source = data.get("source")
        writes_raw = data.get("writes")
        writes = (
            WritesBinding.from_dict(writes_raw, f"{where}.writes")
            if writes_raw is not None
            else None
        )

        on_unavailable = data.get("on_unavailable", "false")
        _require(
            on_unavailable in ("false", "hold"),
            f"{where}: invalid on_unavailable '{on_unavailable}'",
        )

        true_states_raw = data.get("true_states")
        true_states: tuple[str, ...] | None = None
        if true_states_raw is not None:
            _require(
                isinstance(true_states_raw, list)
                and all(isinstance(s, str) for s in true_states_raw),
                f"{where}: 'true_states' must be a list of strings",
            )
            true_states = tuple(true_states_raw)

        attribute = data.get("attribute")
        if attribute is not None:
            _require(
                isinstance(attribute, str) and attribute,
                f"{where}: 'attribute' must be a non-empty string",
            )

        if kind == "input":
            _require(
                isinstance(source, str) and source,
                f"{where}: input tags need a 'source' entity_id",
            )
        else:
            _require(source is None, f"{where}: only input tags may have a 'source'")
            _require(
                true_states is None,
                f"{where}: only input tags may have 'true_states'",
            )
            _require(
                attribute is None,
                f"{where}: only input tags may have an 'attribute'",
            )
        if kind != "coil":
            _require(writes is None, f"{where}: only coil tags may have 'writes'")
        if writes is not None:
            if type_ == "REAL":
                _require(
                    writes.service is not None,
                    f"{where}: a REAL coil write needs 'service' and 'value_key'",
                )
            else:
                _require(
                    writes.service is None,
                    f"{where}: a BOOL coil write must not set 'service'/'value_key'",
                )
        if kind != "memory":
            _require(
                not data.get("retain", False),
                f"{where}: only memory tags may set 'retain'",
            )

        return cls(
            name=name,
            kind=kind,
            type=type_,
            source=source,
            writes=writes,
            retain=bool(data.get("retain", False)),
            on_unavailable=on_unavailable,
            true_states=true_states,
            attribute=attribute,
        )


# --- Function-block instances ----------------------------------------------


@dataclass(slots=True)
class FunctionBlock:
    """A stateful function-block instance declaration (edge / timer / counter).

    ``type`` selects the block; ``params`` holds its configuration (e.g. a timer
    preset). Instance state lives at runtime, not here — the engine threads it
    through :func:`scan.evaluate`.
    """

    type: str
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, **self.params}

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> FunctionBlock:
        where = f"fb '{name}'"
        type_ = _get(data, "type", where)
        _require(
            type_ in KNOWN_FB_TYPES,
            f"{where}: unknown function-block type '{type_}'",
        )
        params = {k: v for k, v in data.items() if k != "type"}
        if type_ in TIMER_TYPES:
            preset = params.get("preset_ms")
            _require(
                isinstance(preset, int) and not isinstance(preset, bool) and preset > 0,
                f"{where}: a timer needs a positive integer 'preset_ms'",
            )
        elif type_ in COUNTER_TYPES:
            pv = params.get("pv")
            _require(
                isinstance(pv, int) and not isinstance(pv, bool) and pv > 0,
                f"{where}: a counter needs a positive integer 'pv'",
            )
            ref = "reset" if type_ == "CTU" else "load"
            val = params.get(ref)
            _require(
                val is None or (isinstance(val, str) and val),
                f"{where}: counter '{ref}' must be a tag name",
            )
        elif type_ in LATCH_TYPES:
            reset = params.get("reset")
            _require(
                isinstance(reset, str) and reset,
                f"{where}: a latch needs a 'reset' tag name",
            )
        return cls(type=type_, params=params)


# --- Rung elements ----------------------------------------------------------


@dataclass(slots=True)
class Contact:
    tag: str
    mode: ContactMode = "NO"

    def to_dict(self) -> dict[str, Any]:
        return {"type": "contact", "tag": self.tag, "mode": self.mode}


@dataclass(slots=True)
class Branch:
    """A parallel branch: OR of one or more series sub-chains."""

    paths: list[list[Element]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"branch": [[el.to_dict() for el in path] for path in self.paths]}


@dataclass(slots=True)
class Not:
    """Inline power inverter: flips the running series power at its position.

    A standalone element (like :class:`FbRef`), with no inner series. In a
    left-to-right series solve it inverts the accumulated power, so ``( a OR b )``
    followed by ``NOT`` conducts NOR. To negate a single contact, use an NC
    contact instead.
    """

    def to_dict(self) -> dict[str, Any]:
        return {"type": "not"}


@dataclass(slots=True)
class Compare:
    """A comparison element: conducts when ``left <op> right`` holds.

    ``left`` is always a REAL tag; ``right`` is either a numeric constant or the
    name of another REAL tag. Stateless — it behaves like a contact in a series.
    """

    op: CompareOp
    left: str
    right: float | int | str

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "compare",
            "op": self.op,
            "left": self.left,
            "right": self.right,
        }


@dataclass(slots=True)
class FbRef:
    """Inline reference to a function-block instance in a rung series.

    Its input is the rung power reaching it (the CLK/IN); it conducts on the
    block's output ``Q``. Only valid at the top level of a rung series — not
    inside a branch or ``NOT`` — so left-to-right power stays well defined.
    """

    instance: str

    def to_dict(self) -> dict[str, Any]:
        return {"type": "fb", "instance": self.instance}


Element = Contact | Branch | Not | Compare | FbRef


def _compare_from_dict(data: dict[str, Any], where: str) -> Compare:
    op = _get(data, "op", where)
    _require(op in COMPARE_OPS, f"{where}: invalid compare op '{op}'")
    left = _get(data, "left", where)
    _require(
        isinstance(left, str) and left, f"{where}: compare 'left' must be a tag name"
    )
    _require("right" in data, f"{where}: missing required key 'right'")
    right = data["right"]
    # bool is a subclass of int, so exclude it explicitly.
    _require(
        isinstance(right, (int, float, str)) and not isinstance(right, bool),
        f"{where}: compare 'right' must be a number or a tag name",
    )
    if isinstance(right, str):
        _require(right != "", f"{where}: compare 'right' tag name must be non-empty")
    return Compare(op=op, left=left, right=right)


def _element_from_dict(data: dict[str, Any], where: str) -> Element:
    _require(isinstance(data, dict), f"{where}: element must be an object")
    if data.get("type") == "not":
        return Not()
    if "branch" in data:
        paths_raw = data["branch"]
        _require(
            isinstance(paths_raw, list) and paths_raw,
            f"{where}: 'branch' must be a non-empty list",
        )
        paths: list[list[Element]] = []
        for i, path_raw in enumerate(paths_raw):
            _require(
                isinstance(path_raw, list) and path_raw,
                f"{where}.branch[{i}]: must be a non-empty list",
            )
            paths.append(
                [
                    _element_from_dict(e, f"{where}.branch[{i}][{j}]")
                    for j, e in enumerate(path_raw)
                ]
            )
        return Branch(paths=paths)

    if data.get("type") == "compare":
        return _compare_from_dict(data, where)

    if data.get("type") == "fb":
        instance = _get(data, "instance", where)
        _require(
            isinstance(instance, str) and instance,
            f"{where}: fb 'instance' must be a non-empty string",
        )
        return FbRef(instance=instance)

    _require(
        data.get("type") == "contact",
        f"{where}: unknown element type '{data.get('type')}'",
    )
    tag = _get(data, "tag", where)
    _require(isinstance(tag, str) and tag, f"{where}: 'tag' must be a non-empty string")
    mode = data.get("mode", "NO")
    _require(mode in ("NO", "NC"), f"{where}: invalid contact mode '{mode}'")
    return Contact(tag=tag, mode=mode)


@dataclass(slots=True)
class Coil:
    tag: str
    mode: CoilMode = "="

    def to_dict(self) -> dict[str, Any]:
        return {"type": "coil", "tag": self.tag, "mode": self.mode}

    @classmethod
    def from_dict(cls, data: dict[str, Any], where: str) -> Coil:
        tag = _get(data, "tag", where)
        _require(
            isinstance(tag, str) and tag, f"{where}: 'tag' must be a non-empty string"
        )
        mode = data.get("mode", "=")
        _require(mode in ("=", "S", "R"), f"{where}: invalid coil mode '{mode}'")
        return cls(tag=tag, mode=mode)


@dataclass(slots=True)
class Move:
    """Copy a REAL value into a REAL destination tag when the rung conducts.

    The analog counterpart of a coil. ``src`` is a numeric constant, a REAL tag,
    or a function-block numeric output (``instance.ET`` / ``instance.CV``); ``dst``
    is a writable REAL tag (``memory`` / ``temp``). Like an unwritten coil, ``dst``
    keeps its previous value on a scan where the rung does not conduct.
    """

    dst: str
    src: float | int | str

    def to_dict(self) -> dict[str, Any]:
        return {"type": "move", "dst": self.dst, "src": self.src}

    @classmethod
    def from_dict(cls, data: dict[str, Any], where: str) -> Move:
        dst = _get(data, "dst", where)
        _require(
            isinstance(dst, str) and dst, f"{where}: move 'dst' must be a tag name"
        )
        _require("src" in data, f"{where}: missing required key 'src'")
        src = data["src"]
        _require(
            isinstance(src, (int, float, str)) and not isinstance(src, bool),
            f"{where}: move 'src' must be a number or a REAL reference",
        )
        if isinstance(src, str):
            _require(src != "", f"{where}: move 'src' reference must be non-empty")
        return cls(dst=dst, src=src)


@dataclass(slots=True)
class Calc:
    """Arithmetic output: ``dst := a <op> b`` when the rung conducts (REAL).

    Like a two-input move. ``op`` is ``ADD`` / ``SUB`` / ``MUL`` / ``DIV``; ``a``
    and ``b`` are each a numeric constant, a REAL tag, or a function-block numeric
    output. A missing/non-numeric operand — or a division by zero — leaves ``dst``
    unchanged (like a move that did not fire).
    """

    op: CalcOp
    dst: str
    a: float | int | str
    b: float | int | str

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "calc",
            "op": self.op,
            "dst": self.dst,
            "a": self.a,
            "b": self.b,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], where: str) -> Calc:
        op = _get(data, "op", where)
        _require(op in CALC_OPS, f"{where}: invalid calc op '{op}'")
        dst = _get(data, "dst", where)
        _require(
            isinstance(dst, str) and dst, f"{where}: calc 'dst' must be a tag name"
        )
        operands: dict[str, float | int | str] = {}
        for key in ("a", "b"):
            _require(key in data, f"{where}: missing required key '{key}'")
            val = data[key]
            _require(
                isinstance(val, (int, float, str)) and not isinstance(val, bool),
                f"{where}: calc '{key}' must be a number or a REAL reference",
            )
            if isinstance(val, str):
                _require(
                    val != "", f"{where}: calc '{key}' reference must be non-empty"
                )
            operands[key] = val
        return cls(op=op, dst=dst, a=operands["a"], b=operands["b"])


@dataclass(slots=True)
class Action:
    """A service-call output: call a Home Assistant service with static data on the
    rising edge of the rung (when it becomes energised).

    Generalises the coil write to an arbitrary one-shot side effect — activate a
    scene (``scene.turn_on``), select an option (``select.select_option``), set a
    preset mode (``climate.set_preset_mode``), etc. ``service`` is ``domain.service``
    and ``data`` is the static service-call payload (e.g. the target ``entity_id``
    plus an ``option`` / ``preset_mode``). Stateless in the engine (no tag); the
    coordinator detects the edge and performs the call.
    """

    service: str  # "domain.service"
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"type": "action", "service": self.service, "data": self.data}

    @classmethod
    def from_dict(cls, data: dict[str, Any], where: str) -> Action:
        service = _get(data, "service", where)
        _require(
            isinstance(service, str) and "." in service,
            f"{where}: action 'service' must be 'domain.service'",
        )
        payload = data.get("data", {})
        _require(
            isinstance(payload, dict),
            f"{where}: action 'data' must be an object",
        )
        return cls(service=service, data=dict(payload))


# A rung output is a boolean coil, a REAL move, a REAL calc, or a service call.
Output = Coil | Move | Calc | Action


def _output_from_dict(data: dict[str, Any], where: str) -> Output:
    _require(isinstance(data, dict), f"{where}: output must be an object")
    if data.get("type") == "move":
        return Move.from_dict(data, where)
    if data.get("type") == "calc":
        return Calc.from_dict(data, where)
    if data.get("type") == "action":
        return Action.from_dict(data, where)
    return Coil.from_dict(data, where)


@dataclass(slots=True)
class Rung:
    id: str
    series: list[Element] = field(default_factory=list)
    coils: list[Output] = field(default_factory=list)
    title: str = ""

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "series": [el.to_dict() for el in self.series],
            "coils": [c.to_dict() for c in self.coils],
        }
        if self.title:
            data["title"] = self.title
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any], where: str) -> Rung:
        rid = _get(data, "id", where)
        series_raw = _get(data, "series", where)
        coils_raw = _get(data, "coils", where)
        _require(
            isinstance(series_raw, list) and series_raw,
            f"{where}: 'series' must be a non-empty list",
        )
        _require(
            isinstance(coils_raw, list) and coils_raw,
            f"{where}: 'coils' must be a non-empty list",
        )
        series = [
            _element_from_dict(e, f"{where}.series[{i}]")
            for i, e in enumerate(series_raw)
        ]
        coils = [
            _output_from_dict(c, f"{where}.coils[{i}]") for i, c in enumerate(coils_raw)
        ]
        return cls(
            id=str(rid), series=series, coils=coils, title=str(data.get("title", ""))
        )


@dataclass(slots=True)
class Network:
    id: str
    rungs: list[Rung] = field(default_factory=list)
    title: str = ""

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "rungs": [r.to_dict() for r in self.rungs],
        }
        if self.title:
            data["title"] = self.title
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any], where: str) -> Network:
        nid = _get(data, "id", where)
        rungs_raw = _get(data, "rungs", where)
        _require(
            isinstance(rungs_raw, list) and rungs_raw,
            f"{where}: 'rungs' must be a non-empty list",
        )
        rungs = [
            Rung.from_dict(r, f"{where}.rungs[{i}]") for i, r in enumerate(rungs_raw)
        ]
        return cls(id=str(nid), rungs=rungs, title=str(data.get("title", "")))


# --- Program ----------------------------------------------------------------


@dataclass(slots=True)
class Program:
    tags: dict[str, Tag] = field(default_factory=dict)
    networks: list[Network] = field(default_factory=list)
    scan_interval_ms: int = 500
    meta: dict[str, Any] = field(default_factory=dict)
    fbs: dict[str, FunctionBlock] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "meta": self.meta,
            "scan_interval_ms": self.scan_interval_ms,
            "tags": {name: tag.to_dict() for name, tag in self.tags.items()},
            "networks": [n.to_dict() for n in self.networks],
        }
        if self.fbs:
            data["fbs"] = {name: fb.to_dict() for name, fb in self.fbs.items()}
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Program:
        _require(isinstance(data, dict), "program: expected an object")
        tags_raw = _get(data, "tags", "program")
        networks_raw = _get(data, "networks", "program")
        _require(isinstance(tags_raw, dict), "program: 'tags' must be an object")
        _require(isinstance(networks_raw, list), "program: 'networks' must be a list")

        scan = data.get("scan_interval_ms", 500)
        _require(
            isinstance(scan, int) and scan > 0,
            "program: 'scan_interval_ms' must be a positive int",
        )

        tags = {
            name: Tag.from_dict(name, tag_data) for name, tag_data in tags_raw.items()
        }
        networks = [
            Network.from_dict(n, f"networks[{i}]") for i, n in enumerate(networks_raw)
        ]

        fbs_raw = data.get("fbs", {})
        _require(isinstance(fbs_raw, dict), "program: 'fbs' must be an object")
        fbs = {
            name: FunctionBlock.from_dict(name, fb_data)
            for name, fb_data in fbs_raw.items()
        }

        program = cls(
            tags=tags,
            networks=networks,
            scan_interval_ms=scan,
            meta=dict(data.get("meta", {})),
            fbs=fbs,
        )
        program._validate_references()
        return program

    def _validate_references(self) -> None:
        """Every tag/instance referenced by an element or coil must be declared."""
        known = set(self.tags)

        for name, fb in self.fbs.items():
            _require(
                name not in known,
                f"fb instance '{name}' clashes with a tag of the same name",
            )
            for tag in _fb_referenced_tags(fb):
                _require(
                    tag in known,
                    f"fb '{name}' references unknown tag '{tag}'",
                )

        def check_real(ref: str, where: str) -> None:
            # A compare operand is a REAL tag, or a function-block numeric output
            # written ``instance.OUTPUT`` (e.g. ``t1.ET``).
            if "." in ref:
                inst, _, out = ref.partition(".")
                _require(
                    inst in self.fbs,
                    f"{where}: compare references unknown fb instance '{inst}'",
                )
                _require(
                    out in _fb_numeric_outputs(self.fbs[inst].type),
                    f"{where}: fb '{inst}' has no numeric output '{out}'",
                )
                return
            _require(ref in known, f"{where}: compare references unknown tag '{ref}'")
            _require(
                self.tags[ref].type == "REAL",
                f"{where}: compare tag '{ref}' must be REAL",
            )

        def check_real_dst(dst_tag: str, kind: str, where: str) -> None:
            # A move/calc destination must be a writable REAL tag.
            _require(
                dst_tag in known,
                f"{where}: {kind} writes to unknown tag '{dst_tag}'",
            )
            dst = self.tags[dst_tag]
            _require(
                dst.kind in ("coil", "memory", "temp"),
                f"{where}: {kind} writes to '{dst_tag}' which is a '{dst.kind}' tag",
            )
            _require(
                dst.type == "REAL",
                f"{where}: {kind} target '{dst_tag}' must be a REAL tag",
            )

        def check_element(el: Element, where: str) -> None:
            """Validate a *nested* element (inside a branch)."""
            if isinstance(el, Contact):
                _require(
                    el.tag in known,
                    f"{where}: contact references unknown tag '{el.tag}'",
                )
            elif isinstance(el, Compare):
                check_real(el.left, where)
                if isinstance(el.right, str):
                    check_real(el.right, where)
            elif isinstance(el, FbRef):
                raise ProgramError(
                    f"{where}: a function block may not appear inside a branch"
                )
            elif isinstance(el, Not):
                pass  # inline power inverter — nothing to validate
            else:  # Branch
                for i, path in enumerate(el.paths):
                    for j, sub in enumerate(path):
                        check_element(sub, f"{where}.branch[{i}][{j}]")

        for n in self.networks:
            for r in n.rungs:
                for i, el in enumerate(r.series):
                    where = f"network '{n.id}' rung '{r.id}' series[{i}]"
                    if isinstance(el, FbRef):
                        _require(
                            el.instance in self.fbs,
                            f"{where}: unknown function-block instance '{el.instance}'",
                        )
                    else:
                        check_element(el, where)
                for c in r.coils:
                    rw = f"network '{n.id}' rung '{r.id}'"
                    if isinstance(c, Action):
                        continue  # a service call references no tags
                    if isinstance(c, Move):
                        check_real_dst(c.dst, "move", rw)
                        if isinstance(c.src, str):
                            check_real(c.src, rw)
                        continue
                    if isinstance(c, Calc):
                        check_real_dst(c.dst, "calc", rw)
                        for operand in (c.a, c.b):
                            if isinstance(operand, str):
                                check_real(operand, rw)
                        continue
                    _require(
                        c.tag in known,
                        f"{rw}: coil references unknown tag '{c.tag}'",
                    )
                    target = self.tags[c.tag]
                    _require(
                        target.kind in ("coil", "memory", "temp"),
                        f"{rw}: coil writes to '{c.tag}' which is a "
                        f"'{target.kind}' tag",
                    )
                    _require(
                        target.type == "BOOL",
                        f"{rw}: coil target '{c.tag}' must be a BOOL tag "
                        f"(use a move for REAL)",
                    )

    # Convenience accessors -------------------------------------------------

    def input_tags(self) -> dict[str, Tag]:
        return {name: t for name, t in self.tags.items() if t.kind == "input"}

    def coil_tags(self) -> dict[str, Tag]:
        return {name: t for name, t in self.tags.items() if t.kind == "coil"}

    def memory_tags(self) -> dict[str, Tag]:
        return {name: t for name, t in self.tags.items() if t.kind == "memory"}

    def temp_tags(self) -> dict[str, Tag]:
        return {name: t for name, t in self.tags.items() if t.kind == "temp"}
