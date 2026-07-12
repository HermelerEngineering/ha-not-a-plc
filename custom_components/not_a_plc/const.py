"""Constants for the Not-a-PLC integration."""

from __future__ import annotations

DOMAIN = "not_a_plc"

# Where the coordinator stores runtime state on hass.data.
DATA_COORDINATOR = "coordinator"

# Default program bundled for phase 0 (self-contained demo, see programs/demo.json).
DEFAULT_PROGRAM_FILE = "demo.json"

# Starter programs offered in the config flow (id -> (label, bundled filename)).
# A new service seeds its own program in .storage from the chosen starter; the
# graphical editor later writes to that same per-entry store.
BUNDLED_PROGRAMS: dict[str, tuple[str, str]] = {
    "daylight": ("Daylight demo (coil follows the sun)", "demo.json"),
    "render": ("Render demo (parallel branch + NC)", "render_demo.json"),
    "thermostat": ("Thermostat (temperature hysteresis)", "thermostat.json"),
}
DEFAULT_STARTER = "daylight"

# Scan-interval presets (milliseconds) offered in the config flow.
SCAN_INTERVAL_PRESETS: tuple[int, ...] = (500, 1000, 2000, 5000, 10000)
DEFAULT_SCAN_INTERVAL_MS = 500

# Config-entry data keys.
CONF_STARTER = "starter_program"
CONF_SCAN_INTERVAL = "scan_interval_ms"

# Soft cap: warn (do not block) once this many services run concurrently, since
# each adds a scan loop. No hard limit — the warning is advisory.
SERVICE_SOFT_CAP = 5

# Per-entry canonical program storage in .storage (one key per config entry).
STORAGE_PROGRAM_PREFIX = "not_a_plc.program"

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
