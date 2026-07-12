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

Phase 1 supports contacts (NO/NC), series/parallel, ``NOT`` groups, and coils
``=`` / ``S`` / ``R``. Later phases extend this file (function blocks, timers).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from .errors import ProgramError

# --- Type aliases -----------------------------------------------------------

TagKind = Literal["input", "coil", "memory"]
TagType = Literal["BOOL", "REAL", "TIME"]
ContactMode = Literal["NO", "NC"]
CoilMode = Literal["=", "S", "R"]
CompareOp = Literal["GT", "GE", "LT", "LE", "EQ", "NE"]
UnavailablePolicy = Literal["false", "hold"]

# Coil modes that the evaluator actually implements.
IMPLEMENTED_COIL_MODES: frozenset[str] = frozenset({"=", "S", "R"})

# Comparison operators the evaluator implements (phase 3).
COMPARE_OPS: frozenset[str] = frozenset({"GT", "GE", "LT", "LE", "EQ", "NE"})


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
    """Executor binding: on write-on-change, actuate a real HA entity."""

    target: str  # entity_id

    def to_dict(self) -> dict[str, Any]:
        return {"target": self.target}

    @classmethod
    def from_dict(cls, data: dict[str, Any], where: str) -> WritesBinding:
        target = _get(data, "target", where)
        _require(
            isinstance(target, str) and target,
            f"{where}: 'target' must be a non-empty entity_id",
        )
        return cls(target=target)


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
        return data

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> Tag:
        where = f"tag '{name}'"
        kind = _get(data, "kind", where)
        _require(kind in ("input", "coil", "memory"), f"{where}: invalid kind '{kind}'")

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
        if kind != "coil":
            _require(writes is None, f"{where}: only coil tags may have 'writes'")

        return cls(
            name=name,
            kind=kind,
            type=type_,
            source=source,
            writes=writes,
            retain=bool(data.get("retain", False)),
            on_unavailable=on_unavailable,
            true_states=true_states,
        )


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
    """Negation: inverts the result of an inner series chain (NOT)."""

    inner: list[Element] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"not": [el.to_dict() for el in self.inner]}


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


Element = Contact | Branch | Not | Compare


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
    if "not" in data:
        inner_raw = data["not"]
        _require(
            isinstance(inner_raw, list) and inner_raw,
            f"{where}: 'not' must be a non-empty list",
        )
        return Not(
            inner=[
                _element_from_dict(e, f"{where}.not[{i}]")
                for i, e in enumerate(inner_raw)
            ]
        )
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
class Rung:
    id: str
    series: list[Element] = field(default_factory=list)
    coils: list[Coil] = field(default_factory=list)
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
            Coil.from_dict(c, f"{where}.coils[{i}]") for i, c in enumerate(coils_raw)
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "meta": self.meta,
            "scan_interval_ms": self.scan_interval_ms,
            "tags": {name: tag.to_dict() for name, tag in self.tags.items()},
            "networks": [n.to_dict() for n in self.networks],
        }

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

        program = cls(
            tags=tags,
            networks=networks,
            scan_interval_ms=scan,
            meta=dict(data.get("meta", {})),
        )
        program._validate_references()
        return program

    def _validate_references(self) -> None:
        """Every tag referenced by a contact or coil must be declared."""
        known = set(self.tags)

        def check_real(tag: str, where: str) -> None:
            _require(tag in known, f"{where}: compare references unknown tag '{tag}'")
            _require(
                self.tags[tag].type == "REAL",
                f"{where}: compare tag '{tag}' must be REAL",
            )

        def check_element(el: Element, where: str) -> None:
            if isinstance(el, Contact):
                _require(
                    el.tag in known,
                    f"{where}: contact references unknown tag '{el.tag}'",
                )
            elif isinstance(el, Compare):
                check_real(el.left, where)
                if isinstance(el.right, str):
                    check_real(el.right, where)
            elif isinstance(el, Not):
                for i, sub in enumerate(el.inner):
                    check_element(sub, f"{where}.not[{i}]")
            else:  # Branch
                for i, path in enumerate(el.paths):
                    for j, sub in enumerate(path):
                        check_element(sub, f"{where}.branch[{i}][{j}]")

        for n in self.networks:
            for r in n.rungs:
                for i, el in enumerate(r.series):
                    check_element(el, f"network '{n.id}' rung '{r.id}' series[{i}]")
                for c in r.coils:
                    _require(
                        c.tag in known,
                        f"network '{n.id}' rung '{r.id}': "
                        f"coil references unknown tag '{c.tag}'",
                    )
                    kind = self.tags[c.tag].kind
                    _require(
                        kind in ("coil", "memory"),
                        f"network '{n.id}' rung '{r.id}': coil writes to "
                        f"'{c.tag}' which is a '{kind}' tag",
                    )

    # Convenience accessors -------------------------------------------------

    def input_tags(self) -> dict[str, Tag]:
        return {name: t for name, t in self.tags.items() if t.kind == "input"}

    def coil_tags(self) -> dict[str, Tag]:
        return {name: t for name, t in self.tags.items() if t.kind == "coil"}

    def memory_tags(self) -> dict[str, Tag]:
        return {name: t for name, t in self.tags.items() if t.kind == "memory"}
