"""Pure engine tests for timer function blocks, driven by a fake clock."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from engine import Program, ProgramError, evaluate

_BASE = datetime(2024, 1, 1, tzinfo=UTC)


def _timer_program(fb_type: str, preset_ms: int) -> Program:
    """One rung: out = <timer>(run)."""
    return Program.from_dict(
        {
            "tags": {
                "run": {"kind": "input", "source": "binary_sensor.run"},
                "out": {"kind": "coil"},
            },
            "fbs": {"t": {"type": fb_type, "preset_ms": preset_ms}},
            "networks": [
                {
                    "id": "n",
                    "rungs": [
                        {
                            "id": "r",
                            "series": [
                                {"type": "contact", "tag": "run"},
                                {"type": "fb", "instance": "t"},
                            ],
                            "coils": [{"type": "coil", "tag": "out"}],
                        }
                    ],
                }
            ],
        }
    )


def _run(program: Program, seq: list[tuple[bool, float]]) -> list[bool]:
    """Replay (input, time-in-seconds) steps against a fake clock."""
    previous: dict[str, bool] | None = None
    fbs: dict[str, dict] = {}
    outs: list[bool] = []
    for run, t in seq:
        now = _BASE + timedelta(seconds=t)
        result = evaluate(program, {"run": run}, now=now, previous=previous, fbs=fbs)
        outs.append(result["out"])
        previous = result
        fbs = result.fbs
    return outs


def test_ton_turns_on_after_the_preset() -> None:
    program = _timer_program("TON", 1000)
    seq = [(True, 0.0), (True, 0.5), (True, 1.0), (True, 2.0), (False, 2.5)]
    assert _run(program, seq) == [False, False, True, True, False]


def test_tof_holds_on_for_the_preset_after_input_drops() -> None:
    program = _timer_program("TOF", 1000)
    seq = [(True, 0.0), (True, 0.5), (False, 1.0), (False, 1.4), (False, 1.6)]
    assert _run(program, seq) == [True, True, True, True, False]


def test_tp_pulses_for_the_preset_on_a_rising_edge() -> None:
    program = _timer_program("TP", 1000)
    seq = [
        (False, 0.0),
        (True, 0.2),
        (True, 0.5),
        (True, 1.5),
        (True, 2.0),
        (False, 2.5),
        (True, 3.0),
    ]
    assert _run(program, seq) == [False, True, True, False, False, False, True]


def test_timer_requires_a_positive_preset() -> None:
    with pytest.raises(ProgramError, match="positive integer 'preset_ms'"):
        _timer_program("TON", 0)


def test_timer_needs_a_clock() -> None:
    program = _timer_program("TON", 1000)
    with pytest.raises(ProgramError, match="needs a clock"):
        evaluate(program, {"run": True})  # now is None
