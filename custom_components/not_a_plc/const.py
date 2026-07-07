"""Constants for the Not a PLC integration."""

from __future__ import annotations

DOMAIN = "not_a_plc"

# Where the coordinator stores runtime state on hass.data.
DATA_COORDINATOR = "coordinator"

# Default program bundled for phase 0 (self-contained demo, see programs/demo.json).
DEFAULT_PROGRAM_FILE = "demo.json"

# Fallback states that a BOOL input treats as True when a tag does not declare its
# own ``true_states``. A tag's ``true_states`` overrides this per input.
DEFAULT_TRUE_STATES: frozenset[str] = frozenset(
    {"on", "true", "home", "open", "detected", "above_horizon", "active", "1"}
)

# Persisted retained state (retain: true memory bits) lives in .storage.
STORAGE_VERSION = 1
STORAGE_KEY_PREFIX = "not_a_plc.retain"

# Debounce for saving retained state after a scan (seconds).
RETAIN_SAVE_DELAY = 10.0
