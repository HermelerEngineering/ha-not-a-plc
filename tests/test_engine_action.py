"""Pure engine tests for the service-call (Action) output.

The engine only surfaces a per-rung *energised level* on ``ScanResult.actions``;
the rising-edge detection and the actual service call live in the coordinator
(HA layer). These tests pin the level the coordinator relies on.
"""

from __future__ import annotations

from engine import Program, evaluate


def _action_program() -> Program:
    return Program.from_dict(
        {
            "tags": {"en": {"kind": "input", "source": "binary_sensor.en"}},
            "networks": [
                {
                    "id": "n1",
                    "rungs": [
                        {
                            "id": "r1",
                            "series": [{"type": "contact", "tag": "en"}],
                            "coils": [
                                {
                                    "type": "action",
                                    "service": "scene.turn_on",
                                    "data": {"entity_id": "scene.movie"},
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    )


def test_action_rung_reports_energised_level() -> None:
    program = _action_program()
    assert evaluate(program, {"en": True}).actions == {"n1/r1": True}
    assert evaluate(program, {"en": False}).actions == {"n1/r1": False}


def test_action_output_produces_no_tag_value() -> None:
    # An action is a side effect, not a value — it never lands in the output image.
    result = evaluate(_action_program(), {"en": True})
    assert dict(result) == {}


def test_no_actions_key_for_rungs_without_an_action() -> None:
    program = Program.from_dict(
        {
            "tags": {
                "en": {"kind": "input", "source": "binary_sensor.en"},
                "out": {"kind": "coil"},
            },
            "networks": [
                {
                    "id": "n1",
                    "rungs": [
                        {
                            "id": "r1",
                            "series": [{"type": "contact", "tag": "en"}],
                            "coils": [{"type": "coil", "tag": "out"}],
                        }
                    ],
                }
            ],
        }
    )
    assert evaluate(program, {"en": True}).actions == {}
