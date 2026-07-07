"""Validate example programs against the JSON Schema (dev-only dependency)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

jsonschema = pytest.importorskip("jsonschema")

_ROOT = Path(__file__).parent.parent / "custom_components" / "not_a_plc"
_SCHEMA = json.loads((_ROOT / "schema" / "program.schema.json").read_text())


def _validator():
    return jsonschema.Draft202012Validator(_SCHEMA)


def test_schema_itself_is_valid() -> None:
    jsonschema.Draft202012Validator.check_schema(_SCHEMA)


def test_bundled_demo_matches_schema() -> None:
    demo = json.loads((_ROOT / "programs" / "demo.json").read_text())
    _validator().validate(demo)


def test_invalid_program_is_rejected_by_schema() -> None:
    bad = {
        "tags": {"a": {"kind": "spark"}},  # invalid kind
        "networks": [],
    }
    assert not _validator().is_valid(bad)


def test_phase1_features_match_schema() -> None:
    """S/R coils, NOT groups, retain, and true_states all validate."""
    program = {
        "scan_interval_ms": 500,
        "tags": {
            "a": {
                "kind": "input",
                "type": "BOOL",
                "source": "binary_sensor.a",
                "true_states": ["on", "home"],
            },
            "m": {"kind": "memory", "type": "BOOL", "retain": True},
            "out": {"kind": "coil", "type": "BOOL"},
        },
        "networks": [
            {
                "id": "n1",
                "rungs": [
                    {
                        "id": "r1",
                        "series": [{"not": [{"type": "contact", "tag": "a"}]}],
                        "coils": [{"type": "coil", "tag": "m", "mode": "S"}],
                    },
                    {
                        "id": "r2",
                        "series": [{"type": "contact", "tag": "m"}],
                        "coils": [{"type": "coil", "tag": "out", "mode": "="}],
                    },
                ],
            }
        ],
    }
    _validator().validate(program)
