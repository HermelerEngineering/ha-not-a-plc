"""Pure engine evaluation tests (standard library only)."""

from __future__ import annotations

import pytest

from engine import Program, ProgramError, evaluate


def _and_or_program() -> Program:
    """out = a AND (b OR c)."""
    return Program.from_dict(
        {
            "tags": {
                "a": {"kind": "input", "source": "binary_sensor.a"},
                "b": {"kind": "input", "source": "binary_sensor.b"},
                "c": {"kind": "input", "source": "binary_sensor.c"},
                "out": {"kind": "coil"},
            },
            "networks": [
                {
                    "id": "n1",
                    "rungs": [
                        {
                            "id": "r1",
                            "series": [
                                {"type": "contact", "tag": "a"},
                                {
                                    "branch": [
                                        [{"type": "contact", "tag": "b"}],
                                        [{"type": "contact", "tag": "c"}],
                                    ]
                                },
                            ],
                            "coils": [{"type": "coil", "tag": "out"}],
                        }
                    ],
                }
            ],
        }
    )


@pytest.mark.parametrize(
    ("a", "b", "c", "expected"),
    [
        (False, False, False, False),
        (True, False, False, False),
        (True, True, False, True),
        (True, False, True, True),
        (True, True, True, True),
        (False, True, True, False),
    ],
)
def test_and_or_truth_table(a: bool, b: bool, c: bool, expected: bool) -> None:
    program = _and_or_program()
    assert evaluate(program, {"a": a, "b": b, "c": c})["out"] is expected


def test_normally_closed_contact_inverts() -> None:
    program = Program.from_dict(
        {
            "tags": {
                "a": {"kind": "input", "source": "binary_sensor.a"},
                "out": {"kind": "coil"},
            },
            "networks": [
                {
                    "id": "n",
                    "rungs": [
                        {
                            "id": "r",
                            "series": [{"type": "contact", "tag": "a", "mode": "NC"}],
                            "coils": [{"type": "coil", "tag": "out"}],
                        }
                    ],
                }
            ],
        }
    )
    assert evaluate(program, {"a": False})["out"] is True
    assert evaluate(program, {"a": True})["out"] is False


def test_last_write_wins_on_duplicate_coil() -> None:
    program = Program.from_dict(
        {
            "tags": {
                "x": {"kind": "input", "source": "binary_sensor.x"},
                "y": {"kind": "coil"},
            },
            "networks": [
                {
                    "id": "n",
                    "rungs": [
                        {
                            "id": "first",
                            "series": [{"type": "contact", "tag": "x"}],
                            "coils": [{"type": "coil", "tag": "y"}],
                        },
                        {
                            "id": "second",
                            "series": [{"type": "contact", "tag": "x", "mode": "NC"}],
                            "coils": [{"type": "coil", "tag": "y"}],
                        },
                    ],
                }
            ],
        }
    )
    # second rung (NC) wins: with x True, NC conducts False -> y False.
    assert evaluate(program, {"x": True})["y"] is False


def test_unknown_coil_mode_raises_at_evaluate() -> None:
    """The evaluator guards against a coil mode the model somehow let through."""
    program = _and_or_program()
    # Bypass model validation to simulate an unimplemented/corrupt mode.
    program.networks[0].rungs[0].coils[0].mode = "X"  # type: ignore[assignment]
    with pytest.raises(ProgramError, match="coil mode 'X'"):
        evaluate(program, {"a": True, "b": True, "c": True})


def _sr_latch_program() -> Program:
    """S/R latch on a memory bit that then drives a coil (reset rung last)."""
    return Program.from_dict(
        {
            "tags": {
                "set": {"kind": "input", "source": "binary_sensor.set"},
                "rst": {"kind": "input", "source": "binary_sensor.rst"},
                "m": {"kind": "memory", "retain": True},
                "out": {"kind": "coil"},
            },
            "networks": [
                {
                    "id": "n",
                    "rungs": [
                        {
                            "id": "r1",
                            "series": [{"type": "contact", "tag": "set"}],
                            "coils": [{"type": "coil", "tag": "m", "mode": "S"}],
                        },
                        {
                            "id": "r2",
                            "series": [{"type": "contact", "tag": "rst"}],
                            "coils": [{"type": "coil", "tag": "m", "mode": "R"}],
                        },
                        {
                            "id": "r3",
                            "series": [{"type": "contact", "tag": "m"}],
                            "coils": [{"type": "coil", "tag": "out"}],
                        },
                    ],
                }
            ],
        }
    )


def test_sr_latch_persists_across_scans() -> None:
    program = _sr_latch_program()

    # Pulse set: latch stays on after the pulse ends (retentive).
    out = evaluate(program, {"set": True, "rst": False})
    assert out == {"out": True, "m": True}
    out = evaluate(program, {"set": False, "rst": False}, previous=out)
    assert out == {"out": True, "m": True}

    # Pulse reset: latch clears and stays clear.
    out = evaluate(program, {"set": False, "rst": True}, previous=out)
    assert out == {"out": False, "m": False}
    out = evaluate(program, {"set": False, "rst": False}, previous=out)
    assert out == {"out": False, "m": False}


def test_sr_latch_reset_dominant_when_both() -> None:
    program = _sr_latch_program()
    # Reset rung is last, so it wins when set and reset are both active.
    out = evaluate(program, {"set": True, "rst": True})
    assert out["m"] is False


def test_memory_bit_drives_downstream_coil_same_scan() -> None:
    """A memory bit set by an earlier rung is visible to a later rung's contact."""
    program = _sr_latch_program()
    out = evaluate(program, {"set": True, "rst": False})
    assert out["out"] is True  # r3 saw m=True set by r1 in the same scan


def test_first_scan_without_previous_starts_false() -> None:
    program = _sr_latch_program()
    out = evaluate(program, {"set": False, "rst": False})
    assert out == {"out": False, "m": False}


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        (False, False, True),
        (True, False, False),
        (False, True, False),
        (True, True, False),
    ],
)
def test_not_group_inverts_inner_series(a: bool, b: bool, expected: bool) -> None:
    """NOT( a OR b ) — a negation wrapping a parallel branch."""
    program = Program.from_dict(
        {
            "tags": {
                "a": {"kind": "input", "source": "binary_sensor.a"},
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
                                    "not": [
                                        {
                                            "branch": [
                                                [{"type": "contact", "tag": "a"}],
                                                [{"type": "contact", "tag": "b"}],
                                            ]
                                        }
                                    ]
                                }
                            ],
                            "coils": [{"type": "coil", "tag": "out"}],
                        }
                    ],
                }
            ],
        }
    )
    assert evaluate(program, {"a": a, "b": b})["out"] is expected
