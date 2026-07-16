"""Pure engine model tests (standard library only, no Home Assistant)."""

from __future__ import annotations

import pytest

from engine import Program, ProgramError


def _minimal() -> dict:
    return {
        "tags": {
            "a": {"kind": "input", "source": "binary_sensor.a"},
            "out": {"kind": "coil"},
        },
        "networks": [
            {
                "id": "n1",
                "rungs": [
                    {
                        "id": "r1",
                        "series": [{"type": "contact", "tag": "a"}],
                        "coils": [{"type": "coil", "tag": "out"}],
                    }
                ],
            }
        ],
    }


def test_parses_minimal_program() -> None:
    program = Program.from_dict(_minimal())
    assert set(program.input_tags()) == {"a"}
    assert set(program.coil_tags()) == {"out"}


def test_round_trip_is_lossless() -> None:
    program = Program.from_dict(_minimal())
    again = Program.from_dict(program.to_dict())
    assert again.to_dict() == program.to_dict()


def test_nested_branch_round_trip() -> None:
    data = _minimal()
    data["tags"]["b"] = {"kind": "input", "source": "binary_sensor.b"}
    data["networks"][0]["rungs"][0]["series"] = [
        {
            "branch": [
                [{"type": "contact", "tag": "a", "mode": "NO"}],
                [{"type": "contact", "tag": "b", "mode": "NC"}],
            ]
        }
    ]
    program = Program.from_dict(data)
    assert Program.from_dict(program.to_dict()).to_dict() == program.to_dict()


def test_unknown_tag_reference_rejected() -> None:
    data = _minimal()
    data["networks"][0]["rungs"][0]["series"][0]["tag"] = "ghost"
    with pytest.raises(ProgramError, match="unknown tag"):
        Program.from_dict(data)


def test_input_requires_source() -> None:
    data = _minimal()
    del data["tags"]["a"]["source"]
    with pytest.raises(ProgramError, match="source"):
        Program.from_dict(data)


def test_coil_cannot_write_to_input() -> None:
    data = _minimal()
    data["networks"][0]["rungs"][0]["coils"][0]["tag"] = "a"
    with pytest.raises(ProgramError, match="is a 'input' tag"):
        Program.from_dict(data)


def test_invalid_contact_mode_rejected() -> None:
    data = _minimal()
    data["networks"][0]["rungs"][0]["series"][0]["mode"] = "SOMETIMES"
    with pytest.raises(ProgramError, match="contact mode"):
        Program.from_dict(data)


def test_inline_not_round_trip() -> None:
    data = _minimal()
    data["tags"]["b"] = {"kind": "input", "source": "binary_sensor.b"}
    # ( a OR NC b ) NOT — a branch followed by the inline power inverter.
    data["networks"][0]["rungs"][0]["series"] = [
        {
            "branch": [
                [{"type": "contact", "tag": "a", "mode": "NO"}],
                [{"type": "contact", "tag": "b", "mode": "NC"}],
            ]
        },
        {"type": "not"},
    ]
    program = Program.from_dict(data)
    assert Program.from_dict(program.to_dict()).to_dict() == program.to_dict()


def test_move_output_round_trip() -> None:
    data = _minimal()
    data["tags"]["level"] = {"kind": "memory", "type": "REAL"}
    data["tags"]["sp"] = {"kind": "input", "type": "REAL", "source": "sensor.sp"}
    data["networks"][0]["rungs"][0]["coils"] = [
        {"type": "move", "dst": "level", "src": 42},
        {"type": "move", "dst": "level", "src": "sp"},
    ]
    program = Program.from_dict(data)
    assert Program.from_dict(program.to_dict()).to_dict() == program.to_dict()


def test_inline_not_allowed_inside_a_branch() -> None:
    # The inverter is a leaf, so it is valid nested inside a branch path too.
    data = _minimal()
    data["networks"][0]["rungs"][0]["series"] = [
        {"branch": [[{"type": "contact", "tag": "a"}, {"type": "not"}]]}
    ]
    program = Program.from_dict(data)
    assert Program.from_dict(program.to_dict()).to_dict() == program.to_dict()


def test_sr_coils_and_memory_round_trip() -> None:
    data = _minimal()
    data["tags"]["m"] = {"kind": "memory", "retain": True}
    data["networks"][0]["rungs"][0]["coils"] = [
        {"type": "coil", "tag": "m", "mode": "S"},
        {"type": "coil", "tag": "out", "mode": "="},
    ]
    program = Program.from_dict(data)
    again = Program.from_dict(program.to_dict())
    assert again.to_dict() == program.to_dict()
    assert program.tags["m"].retain is True


def test_true_states_round_trip() -> None:
    data = _minimal()
    data["tags"]["a"]["true_states"] = ["on", "home"]
    program = Program.from_dict(data)
    assert program.tags["a"].true_states == ("on", "home")
    assert Program.from_dict(program.to_dict()).to_dict() == program.to_dict()


def test_true_states_rejected_on_non_input() -> None:
    data = _minimal()
    data["tags"]["out"]["true_states"] = ["on"]
    with pytest.raises(ProgramError, match="only input tags may have 'true_states'"):
        Program.from_dict(data)
