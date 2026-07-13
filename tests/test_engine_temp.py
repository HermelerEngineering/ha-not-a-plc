"""Pure engine tests for the ``temp`` tag kind (scratch, reset each scan)."""

from __future__ import annotations

from engine import Program, evaluate


def _within_scan_program() -> Program:
    """rung1: a => (= t);  rung2: t => (= out).  A temp is visible same-scan."""
    return Program.from_dict(
        {
            "tags": {
                "a": {"kind": "input", "source": "binary_sensor.a"},
                "t": {"kind": "temp"},
                "out": {"kind": "coil"},
            },
            "networks": [
                {
                    "id": "n1",
                    "rungs": [
                        {
                            "id": "r1",
                            "series": [{"type": "contact", "tag": "a"}],
                            "coils": [{"type": "coil", "tag": "t"}],
                        },
                        {
                            "id": "r2",
                            "series": [{"type": "contact", "tag": "t"}],
                            "coils": [{"type": "coil", "tag": "out"}],
                        },
                    ],
                }
            ],
        }
    )


def test_temp_bit_is_visible_to_a_later_rung_same_scan() -> None:
    program = _within_scan_program()
    result = evaluate(program, {"a": True})
    assert result["t"] is True
    assert result["out"] is True

    result = evaluate(program, {"a": False})
    assert result["t"] is False
    assert result["out"] is False


def _retention_program() -> Program:
    """A set input latches both a memory and a temp bit via S coils.

    Memory is retentive across scans; temp is not — it resets to False every
    scan regardless of the previous image.
    """
    return Program.from_dict(
        {
            "tags": {
                "set": {"kind": "input", "source": "binary_sensor.set"},
                "m": {"kind": "memory"},
                "t": {"kind": "temp"},
            },
            "networks": [
                {
                    "id": "n1",
                    "rungs": [
                        {
                            "id": "r1",
                            "series": [{"type": "contact", "tag": "set"}],
                            "coils": [
                                {"type": "coil", "tag": "m", "mode": "S"},
                                {"type": "coil", "tag": "t", "mode": "S"},
                            ],
                        }
                    ],
                }
            ],
        }
    )


def test_temp_resets_each_scan_while_memory_retains() -> None:
    program = _retention_program()

    # Scan 1: the set pulse latches both bits.
    first = evaluate(program, {"set": True})
    assert first["m"] is True
    assert first["t"] is True

    # Scan 2: input low. The memory S-latch holds; the temp bit resets — even
    # though the previous image (which the coordinator threads back in) had it on.
    second = evaluate(program, {"set": False}, previous=first)
    assert second["m"] is True
    assert second["t"] is False
