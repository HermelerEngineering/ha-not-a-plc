"""Pure engine tests for function-block instances (edge detection)."""

from __future__ import annotations

import pytest

from engine import Program, ProgramError, evaluate


def _edge_program(fb_type: str) -> Program:
    """One rung: pulse = <edge>(clk)."""
    return Program.from_dict(
        {
            "tags": {
                "clk": {"kind": "input", "source": "binary_sensor.clk"},
                "out": {"kind": "coil"},
            },
            "fbs": {"e": {"type": fb_type}},
            "networks": [
                {
                    "id": "n",
                    "rungs": [
                        {
                            "id": "r",
                            "series": [
                                {"type": "contact", "tag": "clk"},
                                {"type": "fb", "instance": "e"},
                            ],
                            "coils": [{"type": "coil", "tag": "out"}],
                        }
                    ],
                }
            ],
        }
    )


def _run(program: Program, clks: list[bool]) -> list[bool]:
    previous: dict[str, bool] | None = None
    fbs: dict[str, dict] = {}
    outs: list[bool] = []
    for clk in clks:
        result = evaluate(program, {"clk": clk}, previous=previous, fbs=fbs)
        outs.append(result["out"])
        previous = result
        fbs = result.fbs
    return outs


def test_r_trig_pulses_one_scan_on_rising_edge() -> None:
    program = _edge_program("R_TRIG")
    #        clk:  F      T     T      F      T
    assert _run(program, [False, True, True, False, True]) == [
        False,
        True,
        False,
        False,
        True,
    ]


def test_f_trig_pulses_one_scan_on_falling_edge() -> None:
    program = _edge_program("F_TRIG")
    #        clk:  F      T      T      F     F      T
    assert _run(program, [False, True, True, False, False, True]) == [
        False,
        False,
        False,
        True,
        False,
        False,
    ]


def test_fb_may_not_appear_inside_a_branch() -> None:
    with pytest.raises(ProgramError, match="may not appear inside a branch"):
        Program.from_dict(
            {
                "tags": {
                    "clk": {"kind": "input", "source": "s.clk"},
                    "out": {"kind": "coil"},
                },
                "fbs": {"e": {"type": "R_TRIG"}},
                "networks": [
                    {
                        "id": "n",
                        "rungs": [
                            {
                                "id": "r",
                                "series": [
                                    {"branch": [[{"type": "fb", "instance": "e"}]]}
                                ],
                                "coils": [{"type": "coil", "tag": "out"}],
                            }
                        ],
                    }
                ],
            }
        )


def test_unknown_fb_type_rejected() -> None:
    with pytest.raises(ProgramError, match="unknown function-block type"):
        _edge_program("WOBBLE")


def test_unknown_fb_instance_rejected() -> None:
    with pytest.raises(ProgramError, match="unknown function-block instance"):
        Program.from_dict(
            {
                "tags": {
                    "clk": {"kind": "input", "source": "s.clk"},
                    "out": {"kind": "coil"},
                },
                "fbs": {"e": {"type": "R_TRIG"}},
                "networks": [
                    {
                        "id": "n",
                        "rungs": [
                            {
                                "id": "r",
                                "series": [{"type": "fb", "instance": "missing"}],
                                "coils": [{"type": "coil", "tag": "out"}],
                            }
                        ],
                    }
                ],
            }
        )


def test_fb_name_clashing_with_tag_rejected() -> None:
    with pytest.raises(ProgramError, match="clashes with a tag"):
        Program.from_dict(
            {
                "tags": {
                    "e": {"kind": "input", "source": "s.clk"},
                    "out": {"kind": "coil"},
                },
                "fbs": {"e": {"type": "R_TRIG"}},
                "networks": [
                    {
                        "id": "n",
                        "rungs": [
                            {
                                "id": "r",
                                "series": [{"type": "contact", "tag": "e"}],
                                "coils": [{"type": "coil", "tag": "out"}],
                            }
                        ],
                    }
                ],
            }
        )
