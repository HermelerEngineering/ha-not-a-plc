"""CLOCK source block: local time/date available to comparators."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from engine import Program, ProgramError, evaluate

# 2026-07-20 is a Monday -> WD 1.
MONDAY_2230 = datetime(2026, 7, 20, 22, 30, 15, tzinfo=UTC)


def _program(**compare: object) -> Program:
    """A one-rung program: `[ clock.<left> <op> <right> ]` -> coil `out`."""
    return Program.from_dict(
        {
            "tags": {"out": {"kind": "coil", "type": "BOOL"}},
            "fbs": {"clock": {"type": "CLOCK"}},
            "networks": [
                {
                    "id": "n1",
                    "rungs": [
                        {
                            "id": "r1",
                            "series": [{"type": "compare", **compare}],
                            "coils": [{"type": "coil", "tag": "out"}],
                        }
                    ],
                }
            ],
        }
    )


def test_clock_outputs_are_available_without_placing_an_element() -> None:
    """A declared CLOCK needs no `fb` element: its outputs are read directly."""
    program = _program(op="GE", left="clock.TOD", right=1350)  # 22:30
    assert evaluate(program, {}, now=MONDAY_2230)["out"] is True


def test_each_clock_field_reads_local_wall_time() -> None:
    program = _program(op="EQ", left="clock.H", right=22)
    assert evaluate(program, {}, now=MONDAY_2230)["out"] is True

    for field, value in (
        ("M", 30),
        ("S", 15),
        ("TOD", 22 * 60 + 30),
        ("WD", 1),  # Monday
        ("D", 20),
        ("MO", 7),
        ("Y", 2026),
    ):
        program = _program(op="EQ", left=f"clock.{field}", right=value)
        result = evaluate(program, {}, now=MONDAY_2230)
        assert result["out"] is True, f"clock.{field} should equal {value}"


def test_weekday_is_iso_monday_is_one_sunday_is_seven() -> None:
    program = _program(op="EQ", left="clock.WD", right=7)
    sunday = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
    assert evaluate(program, {}, now=sunday)["out"] is True
    assert evaluate(program, {}, now=MONDAY_2230)["out"] is False


def test_clock_needs_an_injected_time() -> None:
    program = _program(op="GE", left="clock.TOD", right=0)
    with pytest.raises(ProgramError, match="needs a clock"):
        evaluate(program, {}, now=None)


def test_clock_takes_no_parameters() -> None:
    with pytest.raises(ProgramError, match="takes no parameters"):
        Program.from_dict(
            {
                "tags": {"out": {"kind": "coil", "type": "BOOL"}},
                "fbs": {"clock": {"type": "CLOCK", "preset_ms": 100}},
                "networks": [],
            }
        )


def test_clock_has_no_such_output() -> None:
    with pytest.raises(ProgramError, match="has no numeric output"):
        _program(op="EQ", left="clock.NOPE", right=1)


def test_clock_may_not_be_placed_in_a_rung() -> None:
    with pytest.raises(ProgramError, match="is not placed in a rung"):
        Program.from_dict(
            {
                "tags": {"out": {"kind": "coil", "type": "BOOL"}},
                "fbs": {"clock": {"type": "CLOCK"}},
                "networks": [
                    {
                        "id": "n1",
                        "rungs": [
                            {
                                "id": "r1",
                                "series": [{"type": "fb", "instance": "clock"}],
                                "coils": [{"type": "coil", "tag": "out"}],
                            }
                        ],
                    }
                ],
            }
        )


def test_clock_state_is_exposed_for_the_status_view() -> None:
    """The fb state carries the fields, so the coordinator can publish them."""
    program = _program(op="GE", left="clock.TOD", right=0)
    result = evaluate(program, {}, now=MONDAY_2230)
    assert result.fbs["clock"]["tod"] == 1350.0
    assert result.fbs["clock"]["wd"] == 1.0
