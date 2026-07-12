"""Pure engine tests for comparator elements (standard library only)."""

from __future__ import annotations

import pytest

from engine import Program, ProgramError, evaluate


def _compare_program(op: str, right: object) -> Program:
    """One rung: out = (temp <op> right)."""
    return Program.from_dict(
        {
            "tags": {
                "temp": {"kind": "input", "source": "sensor.temp", "type": "REAL"},
                "sp": {"kind": "input", "source": "sensor.sp", "type": "REAL"},
                "out": {"kind": "coil"},
            },
            "networks": [
                {
                    "id": "n",
                    "rungs": [
                        {
                            "id": "r",
                            "series": [
                                {
                                    "type": "compare",
                                    "op": op,
                                    "left": "temp",
                                    "right": right,
                                }
                            ],
                            "coils": [{"type": "coil", "tag": "out"}],
                        }
                    ],
                }
            ],
        }
    )


@pytest.mark.parametrize(
    ("op", "temp", "expected"),
    [
        ("GT", 22.0, True),
        ("GT", 21.0, False),
        ("GE", 21.0, True),
        ("GE", 20.9, False),
        ("LT", 20.0, True),
        ("LT", 21.0, False),
        ("LE", 21.0, True),
        ("LE", 21.1, False),
        ("EQ", 21.0, True),
        ("EQ", 21.5, False),
        ("NE", 21.5, True),
        ("NE", 21.0, False),
    ],
)
def test_compare_against_constant(op: str, temp: float, expected: bool) -> None:
    program = _compare_program(op, 21)
    assert evaluate(program, {"temp": temp, "sp": 0.0})["out"] is expected


def test_compare_against_tag() -> None:
    program = _compare_program("GT", "sp")
    assert evaluate(program, {"temp": 22.0, "sp": 21.0})["out"] is True
    assert evaluate(program, {"temp": 20.0, "sp": 21.0})["out"] is False


def test_compare_missing_or_nonnumeric_operand_does_not_conduct() -> None:
    program = _compare_program("GT", "sp")
    assert evaluate(program, {"temp": 22.0})["out"] is False  # sp missing
    assert evaluate(program, {"sp": 5.0})["out"] is False  # temp missing
    # A bool is not a number here, so it never conducts.
    assert evaluate(program, {"temp": True, "sp": 0.0})["out"] is False


def test_float_constant_round_trips_through_the_model() -> None:
    program = _compare_program("LT", 25.5)
    assert program.to_dict()["networks"][0]["rungs"][0]["series"][0]["right"] == 25.5


def test_compare_requires_real_tags() -> None:
    with pytest.raises(ProgramError, match="must be REAL"):
        Program.from_dict(
            {
                "tags": {
                    "b": {"kind": "input", "source": "binary_sensor.b"},
                    "out": {"kind": "coil"},
                },
                "networks": [
                    {
                        "id": "n",
                        "rungs": [
                            {
                                "id": "r",
                                "series": [
                                    {
                                        "type": "compare",
                                        "op": "GT",
                                        "left": "b",
                                        "right": 1,
                                    }
                                ],
                                "coils": [{"type": "coil", "tag": "out"}],
                            }
                        ],
                    }
                ],
            }
        )


def test_invalid_compare_op_rejected() -> None:
    with pytest.raises(ProgramError, match="invalid compare op"):
        _compare_program("BETWEEN", 5)
