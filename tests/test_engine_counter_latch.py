"""Pure engine tests for counters (CTU/CTD) and latches (SR/RS)."""

from __future__ import annotations

import pytest

from engine import Program, ProgramError, evaluate


def _counter(fb_type: str, pv: int, ref_param: str) -> Program:
    """One rung: out = <counter>(clk), with reset/load bound to tag 'ctrl'."""
    return Program.from_dict(
        {
            "tags": {
                "clk": {"kind": "input", "source": "binary_sensor.clk"},
                "ctrl": {"kind": "input", "source": "binary_sensor.ctrl"},
                "out": {"kind": "coil"},
            },
            "fbs": {"c": {"type": fb_type, "pv": pv, ref_param: "ctrl"}},
            "networks": [
                {
                    "id": "n",
                    "rungs": [
                        {
                            "id": "r",
                            "series": [
                                {"type": "contact", "tag": "clk"},
                                {"type": "fb", "instance": "c"},
                            ],
                            "coils": [{"type": "coil", "tag": "out"}],
                        }
                    ],
                }
            ],
        }
    )


def _latch(fb_type: str) -> Program:
    """One rung: out = <latch>(s), reset bound to tag 'r'."""
    return Program.from_dict(
        {
            "tags": {
                "s": {"kind": "input", "source": "binary_sensor.s"},
                "r": {"kind": "input", "source": "binary_sensor.r"},
                "out": {"kind": "coil"},
            },
            "fbs": {"l": {"type": fb_type, "reset": "r"}},
            "networks": [
                {
                    "id": "n",
                    "rungs": [
                        {
                            "id": "rung",
                            "series": [
                                {"type": "contact", "tag": "s"},
                                {"type": "fb", "instance": "l"},
                            ],
                            "coils": [{"type": "coil", "tag": "out"}],
                        }
                    ],
                }
            ],
        }
    )


def _run(program: Program, names: list[str], seq: list[tuple[bool, ...]]) -> list[bool]:
    previous: dict[str, bool] | None = None
    fbs: dict[str, dict] = {}
    outs: list[bool] = []
    for row in seq:
        image = dict(zip(names, row, strict=True))
        result = evaluate(program, image, previous=previous, fbs=fbs)
        outs.append(result["out"])
        previous = result
        fbs = result.fbs
    return outs


def test_ctu_counts_rising_edges_up_to_pv_and_resets() -> None:
    program = _counter("CTU", 3, "reset")
    seq = [  # (clk, reset)
        (False, False),
        (True, False),  # cv1
        (True, False),  # held, no edge
        (False, False),
        (True, False),  # cv2
        (False, False),
        (True, False),  # cv3 -> Q
        (True, False),
        (False, True),  # reset -> Q off
    ]
    assert _run(program, ["clk", "ctrl"], seq) == [
        False,
        False,
        False,
        False,
        False,
        False,
        True,
        True,
        False,
    ]


def test_ctd_counts_down_from_load_to_zero() -> None:
    program = _counter("CTD", 3, "load")
    seq = [  # (clk, load)
        (False, True),  # load cv=3
        (True, False),  # cv2
        (False, False),
        (True, False),  # cv1
        (False, False),
        (True, False),  # cv0 -> Q
        (False, False),
        (True, False),  # stays 0 -> Q
    ]
    assert _run(program, ["clk", "ctrl"], seq) == [
        False,
        False,
        False,
        False,
        False,
        True,
        True,
        True,
    ]


def test_sr_latch_is_set_dominant() -> None:
    # IEC 61131-3: the first input in the name dominates -> SR set wins.
    program = _latch("SR")
    seq = [  # (s, r)
        (False, False),
        (True, False),  # set
        (False, False),  # hold
        (False, True),  # reset
        (True, True),  # set wins (set-dominant)
        (False, True),  # reset
    ]
    assert _run(program, ["s", "r"], seq) == [False, True, True, False, True, False]


def test_rs_latch_is_reset_dominant() -> None:
    # IEC 61131-3: the first input in the name dominates -> RS reset wins.
    program = _latch("RS")
    seq = [  # (s, r)
        (False, False),
        (True, False),  # set
        (False, False),  # hold
        (False, True),  # reset
        (True, True),  # reset wins (reset-dominant)
        (True, False),  # set
    ]
    assert _run(program, ["s", "r"], seq) == [False, True, True, False, False, True]


def test_counter_requires_positive_pv() -> None:
    with pytest.raises(ProgramError, match="positive integer 'pv'"):
        _counter("CTU", 0, "reset")


def test_latch_requires_a_reset_tag() -> None:
    with pytest.raises(ProgramError, match="latch needs a 'reset' tag"):
        Program.from_dict(
            {
                "tags": {
                    "s": {"kind": "input", "source": "s.s"},
                    "out": {"kind": "coil"},
                },
                "fbs": {"l": {"type": "SR"}},
                "networks": [
                    {
                        "id": "n",
                        "rungs": [
                            {
                                "id": "r",
                                "series": [{"type": "fb", "instance": "l"}],
                                "coils": [{"type": "coil", "tag": "out"}],
                            }
                        ],
                    }
                ],
            }
        )


def test_fb_reference_to_unknown_tag_rejected() -> None:
    with pytest.raises(ProgramError, match="references unknown tag"):
        Program.from_dict(
            {
                "tags": {
                    "s": {"kind": "input", "source": "s.s"},
                    "out": {"kind": "coil"},
                },
                "fbs": {"l": {"type": "SR", "reset": "does_not_exist"}},
                "networks": [
                    {
                        "id": "n",
                        "rungs": [
                            {
                                "id": "r",
                                "series": [{"type": "fb", "instance": "l"}],
                                "coils": [{"type": "coil", "tag": "out"}],
                            }
                        ],
                    }
                ],
            }
        )
