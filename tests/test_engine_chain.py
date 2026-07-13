"""Regression: the motor example chain (RS -> R_TRIG -> TP), driven by a clock.

Reproduces a user report that R_TRIG's pulse (and the TP it feeds) did not seem
to fire while the RS latch worked. This proves the engine chain is correct.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from engine import Program, evaluate

_BASE = datetime(2024, 1, 1, tzinfo=UTC)


def _motor_chain() -> Program:
    return Program.from_dict(
        {
            "tags": {
                "start": {"kind": "input", "source": "input_boolean.start"},
                "stop": {"kind": "input", "source": "input_boolean.stop"},
                "motor": {"kind": "coil"},
                "edge": {"kind": "coil"},
                "beep": {"kind": "coil"},
            },
            "fbs": {
                "latch": {"type": "RS", "reset": "stop"},
                "redge": {"type": "R_TRIG"},
                "pulse": {"type": "TP", "preset_ms": 2000},
            },
            "networks": [
                {
                    "id": "n",
                    "rungs": [
                        {
                            "id": "r1",
                            "series": [
                                {"type": "contact", "tag": "start"},
                                {"type": "fb", "instance": "latch"},
                            ],
                            "coils": [{"type": "coil", "tag": "motor"}],
                        },
                        {
                            "id": "r2",
                            "series": [
                                {"type": "contact", "tag": "motor"},
                                {"type": "fb", "instance": "redge"},
                            ],
                            "coils": [{"type": "coil", "tag": "edge"}],
                        },
                        {
                            "id": "r3",
                            "series": [
                                {"type": "contact", "tag": "edge"},
                                {"type": "fb", "instance": "pulse"},
                            ],
                            "coils": [{"type": "coil", "tag": "beep"}],
                        },
                    ],
                }
            ],
        }
    )


def test_motor_chain_edge_and_pulse_fire() -> None:
    program = _motor_chain()
    previous: dict[str, bool] | None = None
    fbs: dict[str, dict] = {}
    log: list[dict[str, bool]] = []

    for start, stop, t in [
        (False, False, 0.0),
        (True, False, 0.5),  # motor rises -> edge pulse -> TP starts
        (True, False, 1.0),  # motor held -> edge low, TP still running
        (True, False, 1.5),
        (True, False, 3.0),  # TP (2 s) has ended
    ]:
        result = evaluate(
            program,
            {"start": start, "stop": stop},
            now=_BASE + timedelta(seconds=t),
            previous=previous,
            fbs=fbs,
        )
        log.append({k: result[k] for k in ("motor", "edge", "beep")})
        previous = result
        fbs = result.fbs

    assert log[0] == {"motor": False, "edge": False, "beep": False}
    # Motor rises: one-scan edge pulse, TP starts.
    assert log[1] == {"motor": True, "edge": True, "beep": True}
    # Motor held: edge back low, TP still running.
    assert log[2] == {"motor": True, "edge": False, "beep": True}
    assert log[3] == {"motor": True, "edge": False, "beep": True}
    # TP elapsed.
    assert log[4] == {"motor": True, "edge": False, "beep": False}
