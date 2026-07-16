"""Pure engine tests for the MOVE output (copy a REAL value into a REAL tag)."""

from __future__ import annotations

import pytest

from engine import Program, ProgramError, evaluate


def _move_program(dst_kind: str, src: object) -> Program:
    return Program.from_dict(
        {
            "tags": {
                "en": {"kind": "input", "source": "binary_sensor.en"},
                "x": {"kind": "input", "type": "REAL", "source": "sensor.x"},
                "level": {"kind": dst_kind, "type": "REAL"},
            },
            "networks": [
                {
                    "id": "n",
                    "rungs": [
                        {
                            "id": "r",
                            "series": [{"type": "contact", "tag": "en"}],
                            "coils": [{"type": "move", "dst": "level", "src": src}],
                        }
                    ],
                }
            ],
        }
    )


def test_move_copies_a_constant_when_energised() -> None:
    program = _move_program("memory", 42)
    result = evaluate(program, {"en": True})
    assert result["level"] == 42.0


def test_move_copies_a_real_tag_source() -> None:
    program = _move_program("memory", "x")
    result = evaluate(program, {"en": True, "x": 3.5})
    assert result["level"] == 3.5


def test_real_memory_holds_when_the_rung_does_not_conduct() -> None:
    program = _move_program("memory", 42)
    first = evaluate(program, {"en": True})
    assert first["level"] == 42.0
    # Rung low: the move does not fire, and the REAL memory retains its value.
    second = evaluate(program, {"en": False}, previous=first)
    assert second["level"] == 42.0


def test_real_temp_resets_each_scan() -> None:
    program = _move_program("temp", 42)
    first = evaluate(program, {"en": True})
    assert first["level"] == 42.0
    # Temp is scratch: with the rung low it starts from 0.0, not the previous value.
    second = evaluate(program, {"en": False}, previous=first)
    assert second["level"] == 0.0


def test_move_target_must_be_real() -> None:
    # A BOOL destination is rejected at construction.
    with pytest.raises(ProgramError, match="must be a REAL tag"):
        Program.from_dict(
            {
                "tags": {
                    "en": {"kind": "input", "source": "binary_sensor.en"},
                    "flag": {"kind": "memory", "type": "BOOL"},
                },
                "networks": [
                    {
                        "id": "n",
                        "rungs": [
                            {
                                "id": "r",
                                "series": [{"type": "contact", "tag": "en"}],
                                "coils": [{"type": "move", "dst": "flag", "src": 1}],
                            }
                        ],
                    }
                ],
            }
        )


def _calc_program(op: str, a: object, b: object) -> Program:
    return Program.from_dict(
        {
            "tags": {
                "en": {"kind": "input", "source": "binary_sensor.en"},
                "x": {"kind": "input", "type": "REAL", "source": "sensor.x"},
                "out": {"kind": "memory", "type": "REAL"},
            },
            "networks": [
                {
                    "id": "n",
                    "rungs": [
                        {
                            "id": "r",
                            "series": [{"type": "contact", "tag": "en"}],
                            "coils": [
                                {"type": "calc", "op": op, "dst": "out", "a": a, "b": b}
                            ],
                        }
                    ],
                }
            ],
        }
    )


@pytest.mark.parametrize(
    ("op", "a", "b", "expected"),
    [
        ("ADD", 2, 3, 5.0),
        ("SUB", 10, 4, 6.0),
        ("MUL", 6, 7, 42.0),
        ("DIV", 9, 3, 3.0),
    ],
)
def test_calc_arithmetic(op: str, a: int, b: int, expected: float) -> None:
    result = evaluate(_calc_program(op, a, b), {"en": True})
    assert result["out"] == expected


def test_calc_with_a_tag_operand() -> None:
    result = evaluate(_calc_program("MUL", "x", 2), {"en": True, "x": 2.5})
    assert result["out"] == 5.0


def test_calc_divide_by_zero_leaves_dst_unchanged() -> None:
    program = _calc_program("DIV", 5, 0)
    result = evaluate(program, {"en": True})
    assert result["out"] == 0.0  # seed, not written (guarded)


def test_calc_op_must_be_valid() -> None:
    with pytest.raises(ProgramError, match="invalid calc op"):
        _calc_program("POW", 2, 3)


def test_coil_target_must_be_bool() -> None:
    with pytest.raises(ProgramError, match="must be a BOOL tag"):
        Program.from_dict(
            {
                "tags": {
                    "en": {"kind": "input", "source": "binary_sensor.en"},
                    "level": {"kind": "memory", "type": "REAL"},
                },
                "networks": [
                    {
                        "id": "n",
                        "rungs": [
                            {
                                "id": "r",
                                "series": [{"type": "contact", "tag": "en"}],
                                "coils": [{"type": "coil", "tag": "level"}],
                            }
                        ],
                    }
                ],
            }
        )
