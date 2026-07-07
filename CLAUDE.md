# CLAUDE.md — project context for Claude Code

This file gives Claude Code the context to continue this project correctly.
Read it fully before making changes. The authoritative design document is
[`docs/project-plan.md`](docs/project-plan.md); this file is the working summary.

## What this project is

A native Home Assistant custom integration (`domain: not_a_plc`) that runs a
**cyclic, ladder-style logic engine**. Every ~500 ms it reads HA entities into a
frozen process image, solves a program of ladder networks, and publishes the
results as real HA entities.

It is deliberately **not a PLC** — which is now literally the product name,
"Not a PLC". No real-time guarantees, no Modbus, no external runtime, no sidecar
container. Everything runs inside the HA event loop. Never reintroduce those
dependencies, and never claim it *is* a PLC or offers real-time/deterministic
control in code, docs, or UI — the name is a disclaimer, not an aspiration.

## Non-negotiable design invariants

- **The engine is pure and HA-independent.** `custom_components/not_a_plc/engine/`
  must not import `homeassistant`. It is standard-library only. All HA glue lives
  outside `engine/` (coordinator, platforms, config flow).
- **One canonical program model (the IR).** `engine/model.py` is the single source
  of truth for program structure. The text/YAML DSL and the future graphical
  editor are just producers/consumers of the exact dict shape handled by
  `Program.from_dict` / `to_dict`. Keep the JSON Schema in
  `custom_components/not_a_plc/schema/program.schema.json` in sync with the model.
- **A tag's binding is a bare `entity_id` string.** Never store device or friendly
  names in the IR; the frontend resolves those live from the registries.
- **Scan cycle = snapshot → solve → write-on-change.** Inputs are frozen before
  solving. Outputs actuate real entities only on change. Rungs evaluate
  deterministically top-down; if two rungs write the same coil, the last wins.
- **`evaluate` is a pure function and takes `now` as an argument.** Do not read the
  wall clock inside the engine — inject it, so timers stay deterministic to test.
- **English only** in code, identifiers, DSL keys, UI strings and docs. Other
  languages come later via HA translations, not by translating source.
- Design for eventual official HACS inclusion: keep `manifest.json`, `hacs.json`,
  CI (hassfest + HACS action), tests and typing green.

## Layout

```
custom_components/not_a_plc/
  __init__.py         setup: load program, start coordinator, forward platforms
  config_flow.py      single-instance UI setup
  const.py            DOMAIN and constants
  coordinator.py      cyclic tick; snapshot/solve/write-on-change; input mapping
  binary_sensor.py    coils/memory bits as entities under one "Not a PLC" device
  engine/             PURE, HA-independent
    model.py          IR dataclasses + from_dict/to_dict validation
    scan.py           evaluate(program, image, now, previous) -> {coil/memory: bool}
    parser.py         lossless text DSL <-> IR round-trip
    errors.py         ProgramError
  programs/demo.json  phase-0 demo program (coil follows the sun)
  schema/program.schema.json   JSON Schema for the IR
tests/                engine tests (pure) + integration tests (hass fixtures)
docs/project-plan.md  full phased plan and testing strategy
```

## Commands

```bash
pip install -r requirements-dev.txt
ruff check . && ruff format --check .
mypy custom_components/not_a_plc
pytest -q
```

Engine tests are pure standard library. Integration tests use
`pytest-homeassistant-custom-component`; keep its version aligned with the target
Home Assistant version (see `requirements-dev.txt`).

## Current status

**Phase 1 is complete.** Full bit logic on top of phase 0:

- Coils `=` / `S` / `R`. Retentive outputs (`S`/`R`, and any bit not written on a
  scan) carry across scans via `evaluate(program, image, now, previous)` — the
  engine stays a pure function with no hidden state. A coil/memory bit set by an
  earlier rung is visible to a later rung's contact in the same scan.
- `NOT` element (negates an inner series), alongside NO/NC contacts and branches.
- Per-tag input interpretation: optional `true_states` on BOOL inputs (falls back
  to `DEFAULT_TRUE_STATES`), and real `on_unavailable: hold` backed by an input
  history in the coordinator.
- `retain: true` memory bits survive a restart: the coordinator persists them to
  `.storage` (`Store`) and seeds the previous-output image before the first scan.
  Retention lives entirely in the HA layer; the engine knows nothing about it.
- Lossless text DSL ↔ IR round-trip (`engine/parser.py`), stdlib-only.
- Golden-program corpus (`tests/golden/`) with a recorded input→output trace:
  humidity hysteresis (two comparator BOOL inputs → S/R latch → coil).

Comparators (`GT/LT/...`) on `REAL` remain phase 3, so the hysteresis case takes
its two comparisons as BOOL inputs (e.g. HA threshold sensors) and stays pure bit.

## Next task — Phase 2 (graphical status view)

Per `docs/project-plan.md` §5:

- Websocket API: `get_program` + `subscribe_state` (process image after each scan).
- Read-only Lovelace card (Lit/TS, SVG): draw rungs from the IR, colour energised
  elements live.

Carried over (not blocking phase 2):

- The program is still loaded from the bundled `programs/demo.json`. A
  user-editable program in `.storage` (canonical) with lossless YAML export is
  still pending — only *retained bits* use `.storage` so far, not the program.
- A commandable `switch` coil variant for commissioning is still optional.

## Open decisions (do not silently pick)

- **Name.** Decided (2026-07-07): product name **"Not a PLC"**, domain `not_a_plc`,
  entity prefix `not_a_plc_`. "Ladder" is kept only as the term for the logic
  *paradigm* (ladder logic, rungs, coils) and in internal `Ladder*` class names —
  it is not the product name. The outer repo folder is still `ha-ladder` (rename
  to `ha-not-a-plc` on the git remote when convenient).
- **Coil actuation.** Current model: coil publishes as `binary_sensor` + optional
  `writes` executor. A commandable `switch` variant for commissioning is optional.
- **Canonical storage.** Plan leans to JSON in `.storage` as source of truth with
  lossless YAML export. Not yet implemented (phase 1+).
- **manifest/codeowners/URLs** now point at `HermelerEngineering/ha-not-a-plc`.
