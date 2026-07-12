"""Text DSL round-trip and parse tests (standard library only)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from engine import Program, ProgramError, program_from_text, program_to_text

_ROOT = Path(__file__).parent.parent / "custom_components" / "not_a_plc"


def _feature_program() -> Program:
    """A program exercising every DSL feature: NOT, branch, NC, S/R, meta, titles,
    true_states, writes, retain, hold."""
    return Program.from_dict(
        {
            "meta": {"name": "Feature", "note": "has spaces & symbols"},
            "scan_interval_ms": 750,
            "tags": {
                "a": {
                    "kind": "input",
                    "source": "binary_sensor.a",
                    "on_unavailable": "hold",
                    "true_states": ["on", "home"],
                },
                "b": {"kind": "input", "source": "binary_sensor.b"},
                "m": {"kind": "memory", "retain": True},
                "out": {"kind": "coil", "writes": {"target": "switch.out"}},
                "temp": {"kind": "input", "source": "sensor.temp", "type": "REAL"},
                "sp": {"kind": "input", "source": "sensor.sp", "type": "REAL"},
                "hot": {"kind": "coil"},
            },
            "networks": [
                {
                    "id": "n1",
                    "title": "Net one",
                    "rungs": [
                        {
                            "id": "r1",
                            "title": "set",
                            "series": [
                                {
                                    "not": [
                                        {
                                            "branch": [
                                                [
                                                    {
                                                        "type": "contact",
                                                        "tag": "a",
                                                        "mode": "NC",
                                                    }
                                                ],
                                                [{"type": "contact", "tag": "b"}],
                                            ]
                                        }
                                    ]
                                }
                            ],
                            "coils": [{"type": "coil", "tag": "m", "mode": "S"}],
                        },
                        {
                            "id": "r2",
                            "series": [{"type": "contact", "tag": "a"}],
                            "coils": [{"type": "coil", "tag": "m", "mode": "R"}],
                        },
                        {
                            "id": "r3",
                            "series": [{"type": "contact", "tag": "m"}],
                            "coils": [{"type": "coil", "tag": "out"}],
                        },
                        {
                            "id": "r4",
                            "title": "comparators",
                            "series": [
                                {
                                    "type": "compare",
                                    "op": "GT",
                                    "left": "temp",
                                    "right": 21,
                                },
                                {
                                    "type": "compare",
                                    "op": "LT",
                                    "left": "temp",
                                    "right": 25.5,
                                },
                                {
                                    "type": "compare",
                                    "op": "GE",
                                    "left": "temp",
                                    "right": "sp",
                                },
                            ],
                            "coils": [{"type": "coil", "tag": "hot"}],
                        },
                    ],
                }
            ],
        }
    )


def test_round_trip_bundled_demo() -> None:
    demo = json.loads((_ROOT / "programs" / "demo.json").read_text())
    program = Program.from_dict(demo)
    again = program_from_text(program_to_text(program))
    assert again.to_dict() == program.to_dict()


def test_round_trip_feature_program() -> None:
    program = _feature_program()
    again = program_from_text(program_to_text(program))
    assert again.to_dict() == program.to_dict()


def test_text_is_stable_across_a_second_round_trip() -> None:
    program = _feature_program()
    text1 = program_to_text(program)
    text2 = program_to_text(program_from_text(text1))
    assert text1 == text2


def test_multiple_coils_on_one_rung() -> None:
    text = (
        "scan_interval_ms = 500\n\n"
        "tag i = input BOOL source=binary_sensor.i on_unavailable=false\n"
        "tag x = coil BOOL\n"
        "tag y = coil BOOL\n\n"
        "network n\n"
        "  rung r\n"
        "    i => ( = x ) ( = y )\n"
    )
    program = program_from_text(text)
    coils = program.networks[0].rungs[0].coils
    assert [(c.tag, c.mode) for c in coils] == [("x", "="), ("y", "=")]


def test_comments_and_blank_lines_ignored() -> None:
    text = (
        "# a comment\n"
        "scan_interval_ms = 500\n"
        "\n"
        "tag i = input BOOL source=binary_sensor.i on_unavailable=false\n"
        "tag o = coil BOOL\n"
        "\n"
        "network n\n"
        "  # rung follows\n"
        "  rung r\n"
        "    !i => ( = o )\n"
    )
    program = program_from_text(text)
    contact = program.networks[0].rungs[0].series[0]
    assert (contact.tag, contact.mode) == ("i", "NC")  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("text", "match"),
    [
        (
            "scan_interval_ms = 500\ntag i = input BOOL source=s.i\n"
            "network n\n  rung r\n    i ( = o )\n",
            "'=>'",
        ),
        (
            "scan_interval_ms = 500\ntag i = input BOOL source=s.i\n"
            "tag o = coil BOOL\nnetwork n\n  rung r\n    i =>\n",
            "at least one coil",
        ),
        (
            "scan_interval_ms = 500\nrung r\n    i => ( = o )\n",
            "outside of a 'network'",
        ),
        (
            "scan_interval_ms = 500\ntag network = input BOOL source=s.i\n",
            "reserved word",
        ),
    ],
)
def test_parse_errors(text: str, match: str) -> None:
    with pytest.raises(ProgramError, match=match):
        program_from_text(text)


def test_unbalanced_parenthesis_rejected() -> None:
    text = (
        "scan_interval_ms = 500\n"
        "tag a = input BOOL source=s.a\n"
        "tag o = coil BOOL\n"
        "network n\n"
        "  rung r\n"
        "    ( a | => ( = o )\n"
    )
    with pytest.raises(ProgramError):
        program_from_text(text)
