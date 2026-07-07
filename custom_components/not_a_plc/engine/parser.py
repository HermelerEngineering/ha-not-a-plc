"""Text DSL <-> IR round-trip (pure, standard library only).

The DSL is a **1:1 serialisation of the IR graph**, not a separate language, so a
round-trip ``IR -> text -> IR`` is lossless. It exists so programs can live in git
and be reviewed as readable text; the canonical form remains the JSON IR.

Grammar (informal)::

    meta = <json-object>            # only when meta is non-empty
    scan_interval_ms = <int>

    tag <name> = <kind> <TYPE> [source=<id>] [writes=<id>] [retain=<bool>]
                 [on_unavailable=false|hold] [true_states=<json-array>]

    network <id> ["title"]
      rung <id> ["title"]
        <series> => <coil> [<coil> ...]

Where a *series* is space-separated elements (AND); an element is a contact
(``tag`` for NO, ``!tag`` for NC), a parallel branch ``( pathA | pathB )`` (OR),
or a negation ``NOT( series )``; and a *coil* is ``( = tag )`` / ``( S tag )`` /
``( R tag )``.

The parser builds the canonical dict shape and hands it to
:meth:`Program.from_dict`, so it reuses every model validation.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .errors import ProgramError
from .model import Contact, Not, Program

# Words that introduce a statement and therefore cannot be used as tag names.
_RESERVED = frozenset({"meta", "scan_interval_ms", "tag", "network", "rung", "NOT"})

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_TOKEN_RE = re.compile(r"\s*(NOT\b|[()|!]|[A-Za-z_][A-Za-z0-9_]*)")
_COIL_RE = re.compile(r"\(\s*([=SR])\s+([A-Za-z_][A-Za-z0-9_]*)\s*\)")


# --------------------------------------------------------------------------- #
# IR -> text
# --------------------------------------------------------------------------- #


def _ident(name: str, what: str) -> str:
    if not _IDENT_RE.match(name):
        raise ProgramError(
            f"DSL export: {what} '{name}' is not a valid identifier "
            "([A-Za-z_][A-Za-z0-9_]*)"
        )
    return name


def _element_to_text(el: Any) -> str:
    if isinstance(el, Contact):
        return ("!" if el.mode == "NC" else "") + _ident(el.tag, "tag")
    if isinstance(el, Not):
        return f"NOT( {_series_to_text(el.inner)} )"
    # Branch
    return "( " + " | ".join(_series_to_text(p) for p in el.paths) + " )"


def _series_to_text(elements: list[Any]) -> str:
    return " ".join(_element_to_text(e) for e in elements)


def _tag_to_text(name: str, data: dict[str, Any]) -> str:
    parts = [f"tag {_ident(name, 'tag name')} = {data['kind']} {data['type']}"]
    if "source" in data:
        parts.append(f"source={data['source']}")
    if "writes" in data:
        parts.append(f"writes={data['writes']['target']}")
    if "retain" in data:
        parts.append(f"retain={'true' if data['retain'] else 'false'}")
    if "on_unavailable" in data:
        parts.append(f"on_unavailable={data['on_unavailable']}")
    if "true_states" in data:
        parts.append(
            "true_states=" + json.dumps(data["true_states"], separators=(",", ":"))
        )
    return " ".join(parts)


def _header(keyword: str, id_: str, title: str) -> str:
    line = f"{keyword} {_ident(id_, keyword + ' id')}"
    if title:
        line += " " + json.dumps(title)
    return line


def program_to_text(program: Program) -> str:
    """Serialise a program to the text DSL."""
    lines: list[str] = []
    if program.meta:
        lines.append("meta = " + json.dumps(program.meta))
    lines.append(f"scan_interval_ms = {program.scan_interval_ms}")

    lines.append("")
    for name, tag in program.tags.items():
        lines.append(_tag_to_text(name, tag.to_dict()))

    for net in program.networks:
        lines.append("")
        lines.append(_header("network", net.id, net.title))
        for rung in net.rungs:
            lines.append("  " + _header("rung", rung.id, rung.title))
            series = _series_to_text(rung.series)
            coils = " ".join(
                f"( {c.mode} {_ident(c.tag, 'coil tag')} )" for c in rung.coils
            )
            lines.append(f"    {series} => {coils}")

    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# text -> IR
# --------------------------------------------------------------------------- #


def _tokenise(expr: str) -> list[str]:
    tokens: list[str] = []
    pos = 0
    while pos < len(expr):
        if expr[pos].isspace():
            pos += 1
            continue
        m = _TOKEN_RE.match(expr, pos)
        if not m:
            raise ProgramError(f"DSL: cannot tokenise near '{expr[pos:]}'")
        tokens.append(m.group(1))
        pos = m.end()
    return tokens


def _parse_series(tokens: list[str], pos: int) -> tuple[list[dict[str, Any]], int]:
    elements: list[dict[str, Any]] = []
    while pos < len(tokens) and tokens[pos] not in (")", "|"):
        element, pos = _parse_element(tokens, pos)
        elements.append(element)
    if not elements:
        raise ProgramError("DSL: empty series (a rung path needs at least one element)")
    return elements, pos


def _expect(tokens: list[str], pos: int, tok: str) -> int:
    if pos >= len(tokens) or tokens[pos] != tok:
        got = tokens[pos] if pos < len(tokens) else "end of expression"
        raise ProgramError(f"DSL: expected '{tok}' but found '{got}'")
    return pos + 1


def _parse_element(tokens: list[str], pos: int) -> tuple[dict[str, Any], int]:
    tok = tokens[pos]
    if tok == "!":
        pos += 1
        if pos >= len(tokens) or not _IDENT_RE.match(tokens[pos]):
            raise ProgramError("DSL: '!' must be followed by a tag name")
        return {"type": "contact", "tag": tokens[pos], "mode": "NC"}, pos + 1
    if tok == "NOT":
        pos = _expect(tokens, pos + 1, "(")
        inner, pos = _parse_series(tokens, pos)
        pos = _expect(tokens, pos, ")")
        return {"not": inner}, pos
    if tok == "(":
        pos += 1
        paths: list[list[dict[str, Any]]] = []
        path, pos = _parse_series(tokens, pos)
        paths.append(path)
        while pos < len(tokens) and tokens[pos] == "|":
            path, pos = _parse_series(tokens, pos + 1)
            paths.append(path)
        pos = _expect(tokens, pos, ")")
        return {"branch": paths}, pos
    if _IDENT_RE.match(tok):
        return {"type": "contact", "tag": tok, "mode": "NO"}, pos + 1
    raise ProgramError(f"DSL: unexpected token '{tok}'")


def _parse_elements(expr: str) -> list[dict[str, Any]]:
    tokens = _tokenise(expr)
    elements, pos = _parse_series(tokens, 0)
    if pos != len(tokens):
        raise ProgramError(f"DSL: trailing tokens after series: {tokens[pos:]}")
    return elements


def _parse_coils(text: str) -> list[dict[str, Any]]:
    coils = [
        {"type": "coil", "tag": m.group(2), "mode": m.group(1)}
        for m in _COIL_RE.finditer(text)
    ]
    # Everything outside the matched coils must be whitespace.
    if _COIL_RE.sub(" ", text).strip():
        raise ProgramError(f"DSL: malformed coils in '{text.strip()}'")
    if not coils:
        raise ProgramError(f"DSL: a rung needs at least one coil ('{text.strip()}')")
    return coils


def _parse_tag(rest: str, where: str) -> tuple[str, dict[str, Any]]:
    # rest = "<name> = <kind> <TYPE> [key=value ...]"
    name_part, sep, spec = rest.partition("=")
    if not sep:
        raise ProgramError(f"{where}: expected 'tag <name> = <kind> <type> ...'")
    name = name_part.strip()
    if name in _RESERVED:
        raise ProgramError(
            f"{where}: '{name}' is a reserved word and cannot be a tag name"
        )
    fields = spec.split()
    if len(fields) < 2:
        raise ProgramError(f"{where}: a tag needs at least a kind and a type")
    data: dict[str, Any] = {"kind": fields[0], "type": fields[1]}
    for kv in fields[2:]:
        key, eq, value = kv.partition("=")
        if not eq:
            raise ProgramError(f"{where}: expected key=value, got '{kv}'")
        if key == "source":
            data["source"] = value
        elif key == "writes":
            data["writes"] = {"target": value}
        elif key == "retain":
            data["retain"] = value == "true"
        elif key == "on_unavailable":
            data["on_unavailable"] = value
        elif key == "true_states":
            data["true_states"] = json.loads(value)
        else:
            raise ProgramError(f"{where}: unknown tag field '{key}'")
    return name, data


def _parse_header(rest: str, where: str) -> tuple[str, str]:
    # rest = "<id> [\"title\"]"
    parts = rest.split(None, 1)
    if not parts:
        raise ProgramError(f"{where}: missing id")
    id_ = parts[0]
    title = ""
    if len(parts) > 1:
        try:
            title = json.loads(parts[1])
        except json.JSONDecodeError as err:
            raise ProgramError(f"{where}: title must be a quoted string") from err
        if not isinstance(title, str):
            raise ProgramError(f"{where}: title must be a quoted string")
    return id_, title


def program_from_text(text: str) -> Program:
    """Parse the text DSL into a validated :class:`Program`."""
    data: dict[str, Any] = {
        "meta": {},
        "scan_interval_ms": 500,
        "tags": {},
        "networks": [],
    }
    current_net: dict[str, Any] | None = None
    current_rung: dict[str, Any] | None = None

    for lineno, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        where = f"line {lineno}"
        head = stripped.split(None, 1)[0]
        rest = stripped[len(head) :].strip()

        if head == "meta":
            data["meta"] = json.loads(rest.partition("=")[2].strip())
        elif head == "scan_interval_ms":
            data["scan_interval_ms"] = int(rest.partition("=")[2].strip())
        elif head == "tag":
            name, tag_data = _parse_tag(rest, where)
            data["tags"][name] = tag_data
        elif head == "network":
            nid, title = _parse_header(rest, where)
            current_net = {"id": nid, "rungs": []}
            if title:
                current_net["title"] = title
            data["networks"].append(current_net)
            current_rung = None
        elif head == "rung":
            if current_net is None:
                raise ProgramError(f"{where}: 'rung' outside of a 'network'")
            rid, title = _parse_header(rest, where)
            current_rung = {"id": rid, "series": [], "coils": []}
            if title:
                current_rung["title"] = title
            current_net["rungs"].append(current_rung)
        else:
            # A rung body: "<series> => <coils>"
            if current_rung is None:
                raise ProgramError(f"{where}: rung body outside of a 'rung'")
            left, sep, right = stripped.partition("=>")
            if not sep:
                raise ProgramError(f"{where}: a rung body needs '=>' before its coils")
            if current_rung["series"]:
                raise ProgramError(f"{where}: a rung may only have one body line")
            current_rung["series"] = _parse_elements(left)
            current_rung["coils"] = _parse_coils(right)

    return Program.from_dict(data)
