"""The ladder-logic engine (Home Assistant independent).

Public API:
    Program        -- the in-memory IR, with from_dict/to_dict
    evaluate       -- pure scan-cycle solver
    ProgramError   -- raised on parse/validate/evaluate errors
    program_to_text / program_from_text -- lossless text DSL round-trip
"""

from __future__ import annotations

from .errors import ProgramError
from .model import Program
from .parser import program_from_text, program_to_text
from .scan import evaluate

__all__ = [
    "Program",
    "ProgramError",
    "evaluate",
    "program_from_text",
    "program_to_text",
]
