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

## Phase 2 is complete — graphical status view (read-only), live in HA

Per `docs/project-plan.md` §5. **Backend (this repo):**

- Websocket API (`websocket_api.py`), registered once in `async_setup`:
  - `not_a_plc/get_program` returns the canonical IR (`Program.to_dict`).
  - `not_a_plc/subscribe_state` streams the full process image (inputs + memory +
    coils) after each scan via `coordinator.async_add_listener`, and pushes the
    current image once on subscribe.
- The coordinator freezes the last input snapshot and exposes the merged image
  through `coordinator.state_image()`.
- Tests in `tests/test_websocket.py` drive the handlers with a fake connection
  (no HTTP server — avoids the phcc lingering-thread teardown check). HA-dependent,
  so in `collect_ignore`.

**Frontend card — separate repo `ha-not-a-plc-card`** (Lit/TS, HACS *Dashboard*):

- Pure `computePowerFlow(program, state) → energised elements` (`src/power-flow.ts`),
  unit-tested with vitest; SVG renderer in `src/render.ts`; the `not-a-plc-card`
  element (`src/ladder-card.ts`) calls `get_program` + subscribes to state.
- Live-verified in HA: the demo rung renders and colours correctly (energised uses
  the theme's `--state-active-color`, amber in the default dark theme, not literal
  green).

**Both repos ship as HACS custom repositories.** Tagged `v0.0.1` releases; a `v*`
tag runs each repo's release workflow (the card builds `not-a-plc-card.js` and
attaches it as the release asset HACS installs). Owner: `HermelerEngineering`.

## Phase 2A is complete — multiple services + status-view polish

Per `docs/project-plan.md` §5. **Everything via the UI — no YAML/JSON.**

- **Multiple services (integration v0.1.0):** the config flow creates a named
  service each time (no single-instance limit), picking a bundled starter program
  and a scan-interval preset in the UI. Each service = own device (named after the
  service, so entities are `binary_sensor.<service>_<tag>`), own program, entities
  and scan loop. Advisory scan-load warning past `SERVICE_SOFT_CAP`, no hard limit.
- **Per-service programs in `.storage`:** each entry seeds its canonical program
  once from the chosen starter (`__init__._async_load_program`, key
  `not_a_plc.program.<entry_id>`), cleaned up on removal. The phase-4 editor will
  write to this same store. Second bundled example: `render_demo.json`.
- **Websocket API targets by `entry_id`:** new `not_a_plc/list_services`;
  `get_program`/`subscribe_state` take an optional `entry_id` (omitted = first).
- **Card service selector (card v0.1.0):** `service` config option + a config
  editor (`not-a-plc-card-editor`) dropdown from `list_services`; re-subscribes on
  change. Plus the earlier polish (`( )` coils, rail-aligned stub, larger fonts,
  client-side heartbeat).

## Current task — Phase 3 (extended function blocks)

Per `docs/project-plan.md` §5.

- **Comparators — done (v0.2.0).** A stateless `compare` element conducts when
  `left <op> right` (`GT/GE/LT/LE/EQ/NE`); `left` is a REAL tag, `right` a numeric
  constant or another REAL tag. In `engine/model.py` (`Compare`), `engine/scan.py`
  (`_eval_compare`; missing/non-numeric operand does not conduct), the DSL
  (`[ left OP right ]`, lossless), the JSON schema, and the card (render box +
  `compareConducts`). Golden: `analog_hysteresis` (REAL compares → S/R → coil).
- **Comparators usable via UI — done (v0.3.0).** A `thermostat` bundled starter
  (comparator hysteresis) plus an **options flow** (`LadderOptionsFlow`) that
  rebinds each `input` tag to an entity (REAL → numeric domains) and sets the scan
  interval, writing the updated program back to that service's `.storage` program
  and reloading (entry update listener). This is the stopgap authoring path until
  the phase-4 editor; it does not add/remove logic, only rebinds inputs + interval.
- **`fbs` foundation + edge detect — done (v0.4.0).** A top-level `fbs` declares
  stateful instances; an inline `fb` element (`FbRef`) references one, taking the
  rung power as its input and conducting on its output `Q`. Valid only at the top
  level of a rung (not inside a branch/NOT) so left-to-right power is well defined.
  `evaluate` now returns a `ScanResult` (a dict of outputs **plus** `.fbs` state)
  and takes the previous `fbs` state — still pure, state threaded in/out. The
  coordinator holds fb state in RAM (no per-scan disk) and publishes each block's
  `Q` in `state_image` so the card colours fb elements (which read Q from state).
  DSL: `fb <name> = <TYPE>` + `@instance`. `R_TRIG`/`F_TRIG` implemented; golden
  `edge_detect`. Card renders fb as a labelled box (card v0.3.0).
## Next task — timers, then counters/latches (build on `fbs`)

See `docs/project-plan.md` §5 **"Resumption notes — timers, counters, latches"**
for the concrete plan. In short:

- **Do timers `TON`/`TOF`/`TP` first** — single input (`IN` = rung power), so they
  slot into the existing inline `fb` element. Param `preset_ms`; accumulate elapsed
  from the injected `now` (require `now` for timers; test with a fake clock). Add a
  `_solve_fb` branch, extend `KNOWN_FB_TYPES` and the schema `fb` enum, add a golden.
- **Open decision before counters `CTU`/`CTD` and latches `SR`/`RS`:** they need a
  second input (reset / set), but the inline `fb` element carries only one. Resolve
  how extra inputs are supplied — lean to naming them in the instance declaration.
- **Timer `ET` (REAL output):** decide how programs read it — lean to exposing
  `instance.ET` in `state_image` so a `compare` can reference it. Until then ship
  timers with only boolean `Q`.

The card renders any *single-input* fb as a labelled box already, so timers need no
card change; multi-input blocks may.

Carried over (not blocking phase 3):

- Programs are per-service in `.storage` but only *seeded* from bundled starters;
  in-place *editing* (the graphical editor, phase 4) and lossless YAML export are
  still pending. The `temp` tag kind (§9) is not in `engine/model.py` yet.
- A commandable `switch` coil variant for commissioning is still optional.

## Decided for later phases (do not contradict — see `docs/project-plan.md` §9)

- **Tag model → four kinds.** `input`, `coil`, `memory` (retentive across scans;
  `retain` adds across-restart), `temp` (scratch, reset each scan). "static" = a
  non-retained `memory`. Types stay `BOOL`/`REAL`/`TIME`. (`engine/model.py` still
  has three kinds; `temp` is added when the model work lands.)
- **Multiple instances.** One config entry per Not a PLC "service" (own device,
  program, entities, scan loop); soft cap with a scan-load warning, no hard limit.
  This supersedes the current **single-instance** `config_flow.py` — do not
  entrench single-instance. The websocket API + frontend must then target a service
  by `entry_id` (today `websocket_api.py` resolves "the single instance").
- **Editor = full-page panel.** Shipped from the same frontend repo as the card
  (`ha-not-a-plc-card`), reusing the render/power-flow layer; the read-only card
  stays. Not a second card.

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
