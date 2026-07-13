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

## Phase 3 is complete — extended function blocks (v0.7.0)

Per `docs/project-plan.md` §5. All function blocks are in the pure engine, DSL,
schema and card, each with a golden. No card change was needed beyond the fb box
(v0.3.0) — new block types render as a labelled box coloured by `Q`.

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
- **Timers `TON`/`TOF`/`TP` — done (v0.5.0).** Single-input (`IN` = rung power).
  Instance param `preset_ms`; `_solve_fb` accumulates elapsed from the injected
  `now` delta (stores `last_ms`/`et`/`q`; requires `now` — the coordinator always
  passes it, pure tests pass a fake clock). Q only for now; `ET` not yet exposed.
  Golden `off_delay` (TOF run-on). No card change (renders as the fb box). The
  golden trace format now supports an optional per-step `now_ms`.
- **Function-block numeric outputs — done (v0.6.0).** A `compare` operand may be a
  function-block numeric output written `instance.OUTPUT` (e.g. `t1.ET`). Timers
  expose `ET` (elapsed ms). `_solve_rung` injects `instance.ET`/`instance.CV` into
  the scan `values` after solving the block (so a later same-scan compare sees it);
  the coordinator merges them into `state_image` (so the card colours the compare —
  no card change); `_validate_references` allows the dotted operand
  (`_fb_numeric_outputs(type)`); the DSL round-trips dotted operands (`_REF_RE`).
  **Counter `CV` will surface automatically via this same path** once counters land.
- **Counters + latches — done (v0.7.0).** Multi-input via option A: primary input =
  rung power; secondary inputs named as tag refs in the declaration. `CTU`
  (`{pv, reset?}`, `Q = CV ≥ PV`), `CTD` (`{pv, load?}`, `Q = CV ≤ 0`), `SR`
  (set-dominant, `{reset}`), `RS` (reset-dominant, `{reset}`). `_solve_fb` gets
  `values` to read the secondary tag inputs; counters store `cv` so `CV` surfaces
  via the v0.6.0 mechanism. Validation: `pv` positive int; latch needs `reset`;
  referenced tags must exist. Goldens `sr_latch`, `count_up`. No card change.

## Next task — Phase 4 (graphical editor), decomposed

Interaction model **decided: structured first, then drag** (form/menu editor that
writes the IR; the drag-drop canvas is built on top later). Full breakdown in
`docs/project-plan.md` §5 phase 4. Sub-phases:

- **4.0 — Backend `save_program` — done (v0.7.2).** `websocket_api.async_apply_program`
  writes a validated program to the service's `.storage` and reloads it (so the
  editor and the seeded starter share one canonical store). Commands:
  `not_a_plc/save_program` (IR; `@async_response`), `not_a_plc/get_program_text` /
  `save_program_text` (lossless DSL, for YAML/git export & import). Invalid programs
  are rejected via `Program.from_dict`/`program_from_text` → `invalid_program` error.
- **4.1 — Editor panel scaffold — done (int v0.7.3, card v0.3.2).** The integration
  registers a full-page sidebar panel (`_async_register_panel`, best-effort via
  `panel_custom`, `require_admin`, `module_url` → the HACS-served card bundle
  `/hacsfiles/ha-not-a-plc-card/not-a-plc-card.js`). The panel element
  (`not-a-plc-panel` in the card repo, `src/panel.ts`) reuses render/power-flow:
  service selector, a structural program preview, and a **DSL text editor + Save**
  (`get_program_text` → edit → `save_program_text`). Already usable — edit in the
  UI, no `.storage` fiddling. Structured/form editing is 4.2+.
- **4.2 in progress (card v0.4.1, int v0.7.5):** the panel now shows a **live**
  preview (subscribes to `subscribe_state`), and a **tag table** where each `input`
  tag's source is bound via a self-contained native `<input>` + `<datalist>` picker
  (`ha-entity-picker` is a lazy-loaded element not reliably defined in a custom
  panel; the datalist is populated from `hass.states`, filtered to REAL → numeric
  domains / BOOL → boolean domains) with a Save (`save_program`).
  - **`temp` tag kind — done in the engine (int v0.7.5).** `engine/model.py` now has
    all four kinds (`input`/`coil`/`memory`/`temp`). A `temp` bit is scratch: it
    resets to `False` every scan (never seeded from `previous` in `scan.py`), is a
    valid coil write target, is never published as an entity (binary_sensor only
    covers coil+memory) and never persisted; it still surfaces in `state_image` via
    `self.data` so the card can colour it. Schema + validation updated (`retain` is
    now rejected on non-memory tags). Tests: `tests/test_engine_temp.py`.
  - **Tag management — done (card v0.5.0).** The tag table is now a full editor,
    backed by pure unit-tested helpers in the card's `src/tags.ts`: rename a tag
    (`renameTag` rewrites every reference across the IR so it stays valid), change
    kind (input/coil/memory/**temp**; `setKind` drops fields that no longer apply),
    change type (BOOL/REAL/TIME) with entity-domain **type inference** on input
    binding (§3a: binary_sensor/input_boolean → BOOL, numeric sensor → REAL,
    overridable), a per-kind binding column (input → entity picker, coil → optional
    `writes` target, memory → `retain` checkbox), add a default tag, and delete
    (blocked while referenced via `isTagReferenced`). Saves via `save_program`.
  - **Still to do in 4.2:** nothing blocking — 4.2 is essentially complete. Next is
    **4.3** (structured element editing: forms to add/edit contacts, coils, compares
    and fb instances within a rung).
- **4.3** element editing (forms). **4.4** structure + the drag-drop grid canvas.
  **4.5** validation UX + YAML + polish.

### Known issues

- **Websocket flood while a timer runs — fixed (v0.7.4).** `subscribe_state` now
  gates a push on the *significant* state (`_significant_state`, which drops
  `instance.ET` keys), so a timer's ever-climbing ET no longer pushes every scan.
  The payload still carries ET (a snapshot at each push). Booleans, coil/memory,
  inputs and counter `CV` (a step function) still push on change → a timer reaching
  its preset or resetting (a `Q`/coil change) still updates live. *Caveat:* a
  compare that reads `t1.ET` only recolours when some other value changes; and the
  displayed ET value is a snapshot, not continuous. Acceptable for now.
- **R_TRIG "not firing" — not a bug.** Verified by `tests/test_engine_chain.py`
  (RS → R_TRIG → TP): the edge pulses for one scan and the TP starts.
- **Function-block output colouring — fixed (card v0.3.4).** The card's power-flow
  coloured an fb as `poweredIn && Q`, so once the input pulse ended (e.g. a 1-scan
  `R_TRIG`), a still-running `TP`, a set `SR`/`RS`, or a reached `CTU` went grey —
  even though the engine keeps the rung power at `Q` (which outlives the input).
  Fix: `power-flow.ts` `flowElement` now uses `live = conducts` (i.e. `Q`) for fb
  elements, matching the engine, so blocks and everything downstream follow `Q`.
  (A 1-scan `R_TRIG` pulse is still a brief flash — that is correct; a minimum
  visual hold could be added later if wanted.) Also: counter `CV` label moved below
  the box (was over the edge).
- **Editor preview was not live — fixed (card v0.4.0).** The panel now subscribes
  to `subscribe_state` and colours its preview live (was empty/initial state).
- **Double custom-element registration (card repo).** With the panel installed, the
  bundle loads twice (panel `module_url` + the Lovelace card resource), and the Lit
  `@customElement` decorator calls `customElements.define` unconditionally →
  `Failed to execute 'define'… "not-a-plc-card" has already been used`. Fix: guard
  each define so the bundle is idempotent when loaded more than once — e.g. replace
  `@customElement("x")` with a manual `if (!customElements.get("x")) customElements.define("x", C)`
  (or a small `defineOnce` helper) for `not-a-plc-card`, `not-a-plc-card-editor`,
  and `not-a-plc-panel`. Harmless (the first define wins) but noisy in the log.
- **User has additional feedback** on things that aren't quite right yet — collect
  and triage these at the start of the next session, before/with 4.2.

Carried over (fold into phase 4):

- The `temp` tag kind (§9) is now in `engine/model.py` (int v0.7.5); what remains is
  exposing it in the card's tag editor when tag management (4.2) lands.
- A commandable `switch` coil variant for commissioning is still optional.

## Decided for later phases (do not contradict — see `docs/project-plan.md` §9)

- **Tag model → four kinds.** `input`, `coil`, `memory` (retentive across scans;
  `retain` adds across-restart), `temp` (scratch, reset each scan). "static" = a
  non-retained `memory`. Types stay `BOOL`/`REAL`/`TIME`. (`engine/model.py` has all
  four kinds as of int v0.7.5; `temp` resets each scan and is never published/persisted.)
- **Multiple instances.** One config entry per Not a PLC "service" (own device,
  program, entities, scan loop); soft cap with a scan-load warning, no hard limit.
  This supersedes the current **single-instance** `config_flow.py` — do not
  entrench single-instance. The websocket API + frontend must then target a service
  by `entry_id` (today `websocket_api.py` resolves "the single instance").
- **Editor = full-page panel.** Shipped from the same frontend repo as the card
  (`ha-not-a-plc-card`), reusing the render/power-flow layer; the read-only card
  stays. Not a second card.

## Open decisions (do not silently pick)

- **Name.** Decided (2026-07-07); display name updated (2026-07-08). The
  user-visible product name is **"Not-a-PLC"** (hyphenated) everywhere it shows in
  HA and HACS (manifest/hacs.json `name`, config-flow title/default, device
  manufacturer, card name + default header, READMEs). The domain stays `not_a_plc`
  and the entity prefix `not_a_plc_`. The lowercase **"not a PLC"** (spaced) is the
  disclaimer/paradigm phrase and is left as-is. "Ladder" is only the logic paradigm
  term and internal `Ladder*` class names.
- **Coil actuation.** Current model: coil publishes as `binary_sensor` + optional
  `writes` executor. A commandable `switch` variant for commissioning is optional.
- **Canonical storage.** Plan leans to JSON in `.storage` as source of truth with
  lossless YAML export. Not yet implemented (phase 1+).
- **manifest/codeowners/URLs** now point at `HermelerEngineering/ha-not-a-plc`.
