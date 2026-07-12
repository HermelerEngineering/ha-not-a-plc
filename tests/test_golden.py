"""Golden-program corpus: replay recorded input->output traces.

Each ``tests/golden/<name>.json`` is a program IR with a matching
``<name>.trace.json`` recording a sequence of input snapshots and the expected
outputs after each scan (retentive state carries from one step to the next).

Running these in CI is what protects behaviour across refactors and phase
boundaries. Every golden program is also checked for schema validity and a
lossless DSL round-trip.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from engine import Program, evaluate, program_from_text, program_to_text

_GOLDEN = Path(__file__).parent / "golden"
_PROGRAMS = sorted(
    p for p in _GOLDEN.glob("*.json") if not p.name.endswith(".trace.json")
)
_IDS = [p.stem for p in _PROGRAMS]


@pytest.fixture(params=_PROGRAMS, ids=_IDS)
def golden(request: pytest.FixtureRequest) -> tuple[dict, dict]:
    program_path: Path = request.param
    trace_path = program_path.with_suffix(".trace.json")
    program = json.loads(program_path.read_text())
    trace = json.loads(trace_path.read_text())
    return program, trace


def test_golden_trace_replays(golden: tuple[dict, dict]) -> None:
    program_data, trace = golden
    program = Program.from_dict(program_data)

    previous: dict[str, bool] | None = None
    fb_state: dict[str, dict] = {}
    for i, step in enumerate(trace["steps"]):
        # Timers need a clock; a step may carry an absolute time in epoch ms.
        now = (
            datetime.fromtimestamp(step["now_ms"] / 1000, tz=UTC)
            if "now_ms" in step
            else None
        )
        result = evaluate(
            program, step["inputs"], now=now, previous=previous, fbs=fb_state
        )
        assert result == step["outputs"], f"step {i} ({step.get('note', '')})"
        previous = result
        fb_state = result.fbs


def test_golden_round_trips_through_dsl(golden: tuple[dict, dict]) -> None:
    program_data, _ = golden
    program = Program.from_dict(program_data)
    again = program_from_text(program_to_text(program))
    assert again.to_dict() == program.to_dict()


def test_golden_matches_schema(golden: tuple[dict, dict]) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema_path = (
        Path(__file__).parent.parent
        / "custom_components"
        / "not_a_plc"
        / "schema"
        / "program.schema.json"
    )
    schema = json.loads(schema_path.read_text())
    program_data, _ = golden
    jsonschema.Draft202012Validator(schema).validate(program_data)
