"""Engine-level exceptions.

The engine is intentionally free of any Home Assistant dependency, so it raises
its own exception types instead of Home Assistant ones.
"""

from __future__ import annotations


class ProgramError(ValueError):
    """Raised when a program cannot be parsed, validated, or evaluated."""
