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
- **4.2 — done, validated in HA (card v0.5.0, int v0.7.5).** The panel shows a
  **live** preview (subscribes to `subscribe_state`), and a **tag table** where each
  `input` tag's source is bound via a self-contained native `<input>` + `<datalist>`
  picker
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
- **4.3 — in progress (card v0.7.0).** Structured element editing. Pure IR-edit
  helpers in the card's `src/elements.ts` (immutable, index-addressed by
  network/rung/element/coil; unit-tested in `test/elements.test.ts`): add/remove/move
  networks and rungs (+ titles), add/remove/move/update top-level series elements and
  coils, with constructors (`newContact`/`newCompare`/`newFbRef`/`newCoil`/…). The
  panel gained a **"Program" structure editor** (`_renderStructure` etc.): forms to
  edit networks → rungs → series (contact tag+mode, compare left/op/right, fb
  instance) and coils (tag+mode), all saving via `save_program`. The live preview
  updates as you edit. Structure `<select>`s bind `.value` (not just `?selected`) so
  a reordered element keeps its shown value; multiple coils stack downward from the
  baseline (first coil on the line) joined by a vertical bus (fixed in v0.6.1/v0.7.0).
  - **Function-block instances — done (card v0.7.0).** Pure helpers in the card's
    `src/fbs.ts` (unit-tested, `test/fbs.test.ts`): `addFb`/`removeFb`/`renameFb`
    (rewrites every `fb` reference and `instance.ET`/`.CV` compare operand),
    `setFbType` (resets params to the type's defaults), `setFbParam`, `isFbReferenced`,
    plus `FB_TYPES` and a per-type `fbFields` spec (timer `preset_ms`; counter `pv` +
    `reset`/`load` tag; latch `reset` tag; edges none). Panel **"Function blocks"**
    section renders a row per instance (name, type, typed param inputs, delete —
    blocked while referenced). Now timers/counters/latches/edges are fully usable
    from the UI (declare the instance here, then add an `fb` element referencing it).
  - **Nested branch / NOT editing — done (card v0.8.0).** `src/elements.ts` gained a
    `SeriesStep[]` path model (a step is `{index}` into a NOT's inner series or
    `{index, path}` into a branch path) and path-based `addElementIn`/`removeElementIn`/
    `updateElementIn`/`moveElementIn` (the top-level `addElement`/… are now thin
    wrappers with an empty path), plus `newBranch`/`newNot`. The panel's series editor
    is now **recursive** (`_renderSeries(series, ni, ri, steps)`): a branch renders each
    OR-path as a nested series with add/remove-path, a NOT renders its inner series, to
    any depth. `+ FB` is offered only at the top level (the model forbids fb inside a
    branch/NOT). Tests in `test/elements.test.ts`.
  - **4.3 is complete.** Caveat: a freshly-added rung/branch starts empty and only
    becomes *saveable* once it has the minimum contents referencing existing tags —
    save-time validation reports this; friendlier inline validation is 4.5.
  - **NOT redesigned: inline power inverter (int v0.8.0, card v0.9.0).** Decided
    2026-07-14 — the old group/container `NOT( … )` (which wrapped an inner series)
    was low-value (a NOT on one NO contact is just an NC contact) and awkward to edit.
    `Not` is now a **standalone inline element** (like `FbRef`): in a left-to-right
    series fold it inverts the accumulated power, so `( a OR b ) NOT` conducts NOR; to
    negate a single contact use NC. IR shape changed from `{"not": [ …inner… ]}` to
    `{"type": "not"}` (a leaf). Touched the pure engine (`model.py` `Not`, `scan.py`
    `_eval_series`/`_solve_rung` fold, `parser.py` DSL `NOT` bare token, schema) and
    the card (`ir.ts`, `power-flow.ts` fold, `render.ts` inverter box, `panel.ts` leaf
    element, `elements.ts`/`tags.ts`/`fbs.ts` — NOT no longer nests, so
    `SeriesStep.path` is now required). **Breaking IR change:** any stored program using
    the old `{"not":[…]}` shape fails to load — none of the bundled programs/goldens
    used NOT, so only unit tests needed updating; a user's hand-made NOT program in
    `.storage` would need re-authoring (acceptable pre-1.0).
- **4.4 — in progress.** Interaction model **decided 2026-07-14: click-to-place
  first, then drag** — and **route B→C confirmed 2026-07-15**: the editing surface is
  the *live ladder view itself* (not a separate tile grid), reached in stages —
  **B** click-on-live-view, **C** pointer-drag on top.
  - **Stage A (card v0.10.0) — superseded.** A separate DOM tile-based click-to-place
    canvas; proved the pipeline. Its pure helpers carried forward; the tile UI was
    replaced in stage B.
  - **Stage B — done (card v0.11.0).** The canvas is now the **real SVG ladder** with
    an interaction overlay. `render.ts` gained an optional `CanvasEdit` config threaded
    through `renderNetwork`→`renderRung`; when present it draws transparent hit-targets
    over the ladder — element/coil **select boxes** and (when a tool is armed) **insert
    slots** — wired to callbacks. The read-only card passes no `edit`, so its render is
    unchanged (overlay guarded by `if (edit)`, single geometry source = the same
    `measureElement`/`colX`/`rowY`/coil math). Panel: arm a palette tool → click a ＋
    slot to `insertElementIn`/`insertCoil`; in select mode click an element/coil →
    inspector (reuses `_renderSeriesElement`/`_renderCoilEditor`, so a branch opens the
    recursive form editor). `insertElementIn`/`insertCoil` (elements.ts) + `elementLabel`
    (canvas.ts) are unit-tested.
  - **Stage C — done (card v0.15.0).** Pointer-drag to **reorder** top-level series
    elements on the live view. `@pointerdown` on an element hit-target starts a
    potential drag; once the pointer moves past a small threshold a **drop indicator**
    marks the nearest insertion slot and, on release, the element is reordered via
    `moveElementIn`. A press-release without movement falls through to **select** (so a
    plain click still opens the inspector — this replaced the element's `@click`, which
    avoids the pointerup→click race). Pure, unit-tested math in `canvas.ts`
    (`nearestSlot` → slot index from pointer x; `reorderDelta` → the `moveElementIn`
    delta, accounting for the self-removal shift). The renderer reports each rung's
    insertion-slot x-positions and draws the indicator via new optional `CanvasEdit`
    fields (`drag`/`onGeometry`/`onElementPointerDown`), all guarded so the read-only
    card is unchanged; pointer→SVG-user-x uses `getScreenCTM().inverse()`.
  - **Palette drag-to-place — done (card v0.16.0).** Press-drag a palette tool onto the
    ladder to add it: while dragging, the insert slots show and the same drop indicator
    marks the nearest series slot in the rung under the pointer; on release the element
    is inserted there. A coil tool appends to the rung it is dropped on (coil placement
    is append-only, so no per-slot indicator for coils). A press-release without a drag
    still **arms** the tool, so the earlier arm+click flow is unchanged (chips lost their
    `@click`; arm is driven from pointerup-without-move, same pattern as element select).
    Cross-rung/cross-network hit-testing: `onGeometry` now reports each rung's **y-band**
    plus slot x-positions; the panel finds the SVG under the pointer via
    `shadowRoot.elementFromPoint` + a `data-ni` attribute, converts to that SVG's user
    space (`_toUserXY`), and resolves the target with a pure, unit-tested `hitRung`
    (canvas.ts). New `CanvasEdit.placeDrop` drives the indicator (shared with reorder).
    `_geom` is now `Map<ni, RungGeom[]>`, rebuilt each render.
  - **Branch internals inline — select + insert done (card v0.17.0).** Elements *inside*
    a branch are now individually **selectable** on the live view (click → inspector for
    that exact element) and, with an element tool armed, **insertable** into a branch
    path (click a nested ＋ slot). Driven by a new pure, lit-free `src/layout.ts`
    (`measureElement`/`measureSeries` moved out of render.ts + a `walkSeries` that yields
    every element's grid cell and every series' slot columns with its `SeriesStep` path,
    mirroring the painter's branch layout; unit-tested in `test/walk.test.ts`). The
    overlay adds a nested pass after the top-level one (parent-branch box drawn first, so
    child hit-targets sit on top and win the click). `CanvasEdit` now carries `steps` on
    `selected`/`onSelectElement`/`onInsertElement` (top-level passes `[]`) and an
    `allowNestedInsert` flag (nested slots show only for a persistently armed element
    tool, not a palette drag — a drag resolves to the top level only).
  - **Add branch path on the canvas — done (card v0.18.0).** A **"+ path"** control (a
    small circle at each branch's bottom-left, at any depth) adds a new OR-path:
    with an element tool armed it seeds the path with that element, in select mode it
    adds an empty path (fill it via its nested slots). Pure helper `addBranchPath`
    (elements.ts, unit-tested) + `CanvasEdit.onAddPath`. This fixes the reported
    inability to build a 3-paths-high branch on the canvas (clicking below a 2-path
    branch used to land in the bottom path). **Still to do in 4.4:** palette/reorder
    **drag into** branch-path positions (today a drag resolves to the top level only —
    arm+click into paths and the "+ path" control cover authoring meanwhile); drag-
    *reorder* within a branch; remove-path on the canvas (still via the inspector).
  - **Palette drag into branch positions — done (card v0.20.0).** A palette element
    dragged onto the ladder now resolves to *any* insertion slot, top-level or inside a
    branch path (the target slot highlights as you hover). The renderer reports **every**
    insertion slot (with the rank-spread applied) as a `SlotTarget` via `onGeometry`
    (alongside the top-level `slotXs` the reorder/coil drag still use); the panel picks
    the nearest with a pure, unit-tested `nearestTarget` (canvas.ts — nearest x among
    slots whose y-band contains the pointer, deeper wins ties, null over a gap). Drop
    inserts via `insertElementIn(steps, index)` and selects the new element. Coil drags
    still append via `hitRung`. Palette drag into OR positions validated by the user
    2026-07-17.
  - **Reorder-drag into/within a branch — done (card v0.25.0).** Dragging an *existing*
    element now resolves to any insertion slot, top-level or inside a branch, just like the
    palette drag. Elements at **any depth** are draggable (`onElementPointerDown` gained a
    `steps` arg; nested hit-targets are now `@pointerdown` not `@click`); the drag resolves
    a `SlotTarget` via `nearestTarget` over the source rung's slots (nested slot geometry is
    now reported **always**, not only when a tool is armed) and highlights it (`placeDrop`),
    with the source element ghosted (`dragSource`). On drop, a pure, unit-tested
    `moveElementAcross` (elements.ts) removes the source and inserts at the target, correcting
    the target index/path for the self-removal shift and refusing to drop an element into
    itself; a same-series move reduces to a reorder. A press-release without moving still
    selects (two-click popup). Cross-**rung** moves are still out of scope (drag resolves
    within the source rung). Remove-path on the canvas stays **dropped** (inspector only).
- **4.5 — in progress. Inline validation done (card v0.19.0).** A pure, lit-free,
  unit-tested `src/validate.ts` (`validateProgram(program) → ValidationIssue[]`) flags
  the *unambiguously-broken* things (empty/dangling tag & fb references on
  contacts/compares/fb/coil/move/calc, walking into branch paths; a rung with no
  output = warning). The panel shows them in a live `_renderValidation` bar above the
  editor ("✓ No problems found" or an expandable error/warning list with rung
  locations), updating as you edit. Deliberately conservative — it never flags what the
  backend accepts and does NOT check type-level rules (e.g. compare operand must be
  REAL); the backend's `Program.from_dict` stays authoritative on save. Refinements
  (card v0.19.1): empty **OR-path** flagged (the backend rejects it); the save error is
  now readable (`wsErrorMessage` extracts `.message` from HA's `{code,message}` reject —
  was "[object Object]"); and arm+click placing an element/coil now **switches to select
  mode on the new element** (place-then-configure). **Still in 4.5 (deferred):** YAML
  export/import polish (user parked it 2026-07-17); general polish. Plus two validation-
  UX asks from 2026-07-17 (see the editor-UX backlog below): **highlight the erroring
  position with a red background** on the canvas, not just list it in the bar.

### Known issues

- **Wide networks were clipped at a fixed width — fixed (card v0.33.1).** `coilX` already grew
  with the series (`Math.max(colX(endCol)+24, width-rightSpace)`), but the SVG `viewBox` stayed
  at `VIEW_WIDTH` (760), so a rung longer than that had its output column drawn outside the
  viewBox and cut off — with empty space beside it. `renderNetwork` now computes a
  `contentWidth = max(width, …rungRequiredWidth(rung))` (`colX(measureSeries(cols)) + 24 +
  rightSpace`), renders every rung at that width (so a network's outputs stay aligned) and
  returns it as `RenderedNetwork.width`. The panel uses it for the `viewBox` + pixel size
  (× zoom) and the read-only card grows its viewBox to the widest network. Networks never
  shrink below `VIEW_WIDTH`, so small networks keep the output column on the usual line.

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
- **Editor tag table was not editable — fixed (card v0.4.1).** The binding cells
  used HA's `ha-entity-picker`, a lazy-loaded element not reliably defined inside a
  custom panel, so they rendered but couldn't be edited. Replaced with a
  self-contained native `<input list=…>` + `<datalist>` from `hass.states`. Full tag
  management followed in v0.5.0 (see 4.2 above). Validated in HA.
- **Contacts only coloured as far as power reached — fixed (card v0.5.1).** In a
  series chain the renderer coloured element *symbols* by `live` (power reached AND
  conducts), so a false first contact left every later contact grey even when those
  conditions were individually true. Now condition *symbols* (contacts, compares) are
  coloured by their own `conducts` state, so you can see which conditions are already
  satisfied vs. still missing to energise the coil; the *wires* between elements still
  follow actual power flow (`live`), so the line visibly stops at the break and
  nothing downstream of it lights its connecting wire. Render-only change in
  `render.ts` (`drawContact`/`drawCompare`); `computePowerFlow` already exposed both
  `conducts` and `live` per element. Requested 2026-07-13.
- **Double custom-element registration (card repo) — fixed (card v0.14.1).** With the
  panel installed, the bundle loads twice (panel `module_url` + the Lovelace card
  resource), and the Lit `@customElement` decorator called `customElements.define`
  unconditionally → `Failed to execute 'define'… "not-a-plc-card" has already been used`.
  Fixed with a `defineOnce(name)` decorator (`src/define.ts`) that no-ops when the name
  is already registered (first define wins); it replaces `@customElement` on
  `not-a-plc-card`, `not-a-plc-card-editor` and `not-a-plc-panel`. Bundle is now
  idempotent when loaded more than once.
- **User feedback from the phase-2A/3 round — all addressed.** fb output colouring
  (card v0.3.4), ET no longer floods the websocket (v0.7.4), compares/counters show
  their live value, the editor preview is live (v0.4.0) and the tag table is editable
  (v0.4.1) with full tag management (v0.5.0). Nothing outstanding from that round.

Carried over (fold into a later phase):

- **New brand icons** — done (committed `54f5951`, artwork by the user). (Note `logo.png`
  may still equal `icon@2x.png` — HACS brands ideally wants a wider logo, minor.)
- A commandable `switch` coil variant for commissioning is still optional.

## MOVE / CALC outputs — REAL values (intermediate feature, requested 2026-07-16)

The analog counterparts of a coil: a **`move`** output copies a REAL value into a
REAL destination tag when the rung conducts (`( dst := src )` in the DSL), and a
**`calc`** output computes `dst := a <op> b` (`op` ∈ ADD/SUB/MUL/DIV; DSL
`( dst := a + b )` with `+ - * /`). Operands (src / a / b) are each a number, a REAL
tag, or a fb numeric output; a missing operand or divide-by-zero leaves `dst`.

**CALC — done (int v0.10.0, card v0.13.0).** Same shape as MOVE: `Calc` output
(`{"type":"calc","op","dst","a","b"}`), `Output = Coil | Move | Calc`, `CALC_OPS`,
`scan.py` `_apply_calc` + shared `_resolve_operand`, schema `calc` def, DSL round-trip,
validation reuses `check_real_dst`. Card: `CalcEl`/`CalcOp`/`isCalc`, render `dst := a
<op> b` box, palette `+ − × ÷` tools, inspector (dst REAL + a + op + b), reference walks
in tags/fbs cover a/b. Tests: `tests/test_engine_move.py` (calc cases), card
`test/elements.test.ts`.

- **Stage 1 — done (int v0.9.0, card v0.12.0).** Internal REAL destinations only
  (`memory` / `temp` REAL tags). This introduces **REAL outputs into the engine**:
  the output image is now `bool | float` (`scan.py` `OutputValue`, type-aware seeding
  — BOOL→False, REAL→0.0; REAL `memory` retains its float, REAL `temp` resets). Model
  `Move` (`{"type":"move","dst","src"}`) is a rung output alongside `Coil` (`Output =
  Coil | Move`); validation: `move` target must be a REAL writable tag, `src` a number
  / REAL tag / fb numeric output; a `coil` target must be BOOL. Schema `move` def; DSL
  `( dst := src )`; parser splits output groups into coil/move. Coordinator/`Store`
  widened to `Any`; `binary_sensor` now only publishes **BOOL** coil/memory (REAL
  outputs are internal, surfaced in `state_image` only). Card: `MoveEl`/`Output`,
  power-flow keys coils by `Output`, render draws a move as a `dst := src` box, canvas
  palette `:=` tool + inspector (dst = REAL writable, src = value/REAL tag). Tests:
  `tests/test_engine_move.py`, card `test/elements.test.ts`.
- **Stage 2 — done (int v0.11.0, card v0.14.0).** Write a REAL directly to an **HA
  entity** (dimmer / `input_number` / …). `WritesBinding` gained optional
  `service` + `value_key`: a BOOL coil still actuates via `turn_on`/`turn_off`, a REAL
  coil calls its `service` (`domain.service`, e.g. `light.turn_on`) with the value
  under `value_key` (e.g. `brightness_pct`). Model validates a REAL coil write needs
  both, a BOOL coil write neither. Coordinator `_write_on_change` branches on
  `writes.service`. New **`sensor.py` platform** publishes REAL coil+memory tags as
  `sensor` entities (BOOL stay binary_sensor; REAL temp stays internal); registered in
  `PLATFORMS`. DSL: `write_service=` / `write_key=` tag fields (lossless). Card:
  `WritesBinding.service?/value_key?`, `WRITE_DOMAINS` + `defaultRealWrite` (domain →
  service/value_key defaults), and a REAL-coil write editor in the tag table (target
  picker + service + value_key, prefilled on target pick). Tests:
  model/parser round-trips + validation; card `defaultRealWrite`. **Note:** the REAL
  value must match the target's expected range (e.g. `brightness_pct` is 0–100) — use
  a CALC to scale; no auto-scaling. A deadband on REAL writes could be added later.

## Backlog — requested features (recorded 2026-07-16, implement later)

Grouped by a logical phase, with a feasibility note. Nothing here is built yet.

**Phase 5 — extended I/O (new; mostly backend + small card).**
- **Write/activate an entity preset or scene — done (int v0.13.0, card v0.27.0).** A new
  rung output kind **`Action`** (`{"type":"action","service":"domain.service","data":{…}}`)
  calls an HA service with static data on the rung's **rising edge** — activate a scene
  (`scene.turn_on`), select an option (`select.select_option`), set a preset
  (`climate.set_preset_mode`), etc. `Output = Coil | Move | Calc | Action`; `_output_from_dict`
  + schema `action` def + DSL `( do domain.service {json} )`. The engine stays pure: it
  surfaces a per-rung **energised level** on `ScanResult.actions` (keyed `"<net>/<rung>"`)
  for rungs with an action; the coordinator (`_fire_actions`, holding `_prev_actions`)
  detects the false→true edge and calls the service (one-shot, so a scene fires once when
  the condition becomes true). No output-image value (a side effect, not a tag). Card:
  `ActionEl`/`isAction`/`newAction`, render a "do <service>" box (target below), palette
  `do` tool + `+ Service` button, inspector (service + data-JSON), `validate.ts` flags a
  missing `domain.service`, tag/fb reference walks skip actions. Tests: model round-trip +
  validation, DSL round-trip, a pure scan test for the `actions` level; card validate test.
  *Note:* the coordinator edge-detection + service call is HA-side (integration test lives
  under the phcc collect-ignore); verified by the pure `actions`-level test + HA validation.
  **Structured service-call editor — done (card v0.30.0).** The popup editor replaced the
  raw `service` text + JSON-data inputs with **dropdowns**: pick the service from a curated
  list, then the target entity (filtered to the service's domain), then — for option-style
  services — one of the entity's available options read live from `state.attributes`. Backed
  by a pure, lit-free, unit-tested `src/actions.ts` (`ACTION_SERVICES` specs mapping service
  → `{domain, optionsAttr, valueKey}` e.g. `fan.set_preset_mode` → `preset_modes`/`preset_mode`,
  `select.select_option` → `options`/`option`; `actionSpec`, `withService`/`withEntity`/
  `withValue`, `actionEntity`/`actionValue`). A **"Custom…"** entry keeps the old free
  service + raw-JSON escape hatch for anything off-list. `withService` keeps the entity when
  the new domain matches, else clears it. The canvas action box now also shows the chosen
  option value below the entity. Panel reads option lists via `_stateOptions(entity, attr)`.
  (card v0.30.1: inside the popup an element editor's fields stack **vertically**
  (`.modal .el-row` column, full-width inputs) so a wide editor no longer overflows with a
  horizontal scrollbar; the inline structure editor keeps its horizontal row.)
- **Read an entity *attribute*, not just its state — done (int v0.12.0, card v0.26.0).**
  An input tag optionally reads `state.attributes[attribute]` (e.g. a light's `brightness`,
  a climate's `current_temperature`). `model.py` Tag gained `attribute: str | None` (input-
  only; to_dict/from_dict/schema/DSL `attribute=<name>` round-trip); the coordinator's
  `_read_input` reads the attribute when set, via a shared `_coerce_input` (REAL→float,
  BOOL→real bool/number used directly else matched against `true_states`), falling back to
  `on_unavailable` when the attribute is missing/non-numeric. No engine change (attributes
  resolve to the same typed value). Card: `TagDef.attribute?`, a per-input "attribute
  (optional)" field in the tag table (`_setAttribute`), carried by `normaliseForKind`.
  Tests: model round-trip/validation + the parser feature-program covers it.
  **Attribute dropdown — done (card v0.28.0).** The attribute field is now an
  `<input list>` + per-tag `<datalist>` populated from the *chosen source entity's*
  real `state.attributes` keys (`_attributeKeys(source)`), so the user picks an actual
  key instead of guessing/mistyping. This fixes the reported "shows 0" case: HA
  attribute keys are case-sensitive (a light's is `brightness`, not `Brightness`), so a
  mistyped name resolved to `None` → `on_unavailable`. Note brightness is 0–255 (not a
  percentage); scale with a CALC if a % is wanted.
  **Any entity selectable for a REAL input — done (card v0.29.0).** The input source
  picker now lists **all** entities (new `np-all-entities` datalist, `_entityIds(null)`)
  instead of only the type's domains, because a REAL input can read a numeric attribute
  off any entity (e.g. a light's `brightness`) — previously a `light` only appeared while
  the tag type was BOOL. `inferType` now returns `TagType | null`, returning `null` for
  `AMBIGUOUS_DOMAINS` (light/fan/cover/climate/media_player/valve — boolean state but
  numeric attributes), so binding one **keeps** the user's chosen type instead of forcing
  BOOL; `_setSource` uses `inferType(v) ?? tag.type`. Confident domains still infer
  (numeric → REAL, else BOOL).

**Timer durations with units — done (card v0.34.0).** Timer presets are entered and shown
as `500ms` / `5s` / `3m` / `1h` instead of raw ms. `preset_ms` (ms) stays the canonical IR
and the backend is untouched; a pure, lit-free, unit-tested `src/duration.ts`
(`parseDuration` / `formatDuration`) translates for the editor only. A **bare number means
seconds** (sub-second is pointless given the ~500 ms min cycle). `fbFields` gained a
`duration` field kind (timer preset label is now just "preset"); `_renderFbParam` renders a
text input that formats with the largest exactly-dividing unit and **keeps the previous value
on an unparseable entry** (reverts the input) rather than clearing it. The DSL was left
alone (still raw ms).

**Per-type function-block palette — done (card v0.34.0).** The single generic "FB" chip was
replaced by **one chip per type** (`FB_TYPES` → R_TRIG/F_TRIG/TON/TOF/TP/CTU/CTD/SR/RS), so a
specific block can be dragged straight from the toolbar. `Tool` gained `fbType?`, which now
drives everything the old `label === "FB"` check did: `_elementFor` calls `addFb(program,
tool.fbType)` to auto-create an instance **of that type**, the popup opens on placement, and
the top-level-only guards (`allowNestedInsert`, the nested-drop guard, branch-path seeding)
key off `fbType`. FB chips get a smaller font (`.chip.fb`) so `R_TRIG` fits the 46px square.

**Visual optimisation round (render + editor UX; medium).**
- **TIA-style parameter display on blocks (FB / move / calc).**
  - **FB blocks — done (card v0.21.0).** A pure, lit-free, unit-tested `src/block.ts`
    (`fbBlock(instance, def, state) → {title, ins[], outs[]}`, `blockPinRows`) gives each
    fb type its input/output *pins* with a terse role label (IN/PT/Q/ET, CU/R/PV/CV,
    S/R, CLK). `render.ts` `drawFb` now draws a TIA-style box: power pins on the baseline
    (row 0), parameter pins stacked below; the role label sits **inside** the box, the
    setting/bound-tag **left** of an input pin and the live value **right** of an output
    pin (new `pin-l`/`pin-r`/`pin-v-l`/`pin-v-r` text classes in both `ladder-card.ts`
    and `panel.ts` styles). Function blocks now measure **2 cols × 2 rows**
    (`layout.ts` `measureElement`) to give room; edges (single power pin row) still draw
    a compact box centred on the baseline. **Still to do:** move/calc outputs (below), and
    the *visual polish* (spacing/edge-block empty space) once the user eyeballs it.
  - **Move/calc outputs — done (card v0.22.0).** Same TIA face for the REAL outputs in
    the coil column (`block.ts` `outputBlock`): operand inputs on the left (`IN` for move,
    `IN1`/`IN2` for calc, titled by the operator `MOVE`/`ADD`/`SUB`/`MUL`/`DIV`), the
    destination `OUT` on the right, the rung result (enable) into the box top-left corner.
    a rung with a move/calc reserves more right-hand room (`OUTPUT_BLOCK_SPACE`) and the
    coil hit-target/outline is widened to cover the box. `VIEW_WIDTH` bumped 720→760 for
    margin. Unit-tested in `test/block.test.ts`.
  - **Alignment fix (card v0.22.1).** First cut jogged the power line *up* into the box top
    and centred the box on the coil row (misaligned vs the fb blocks). Now the enable runs
    **straight** in along the coil row (the box's top row) and the box hangs below it — same
    as an fb — with operands stacked down the left. This needed the coil column to stack
    **by output height** rather than a fixed `CELL_H` per output: `outputRows(output)` (calc
    = 2 rows, move/coil = 1), a `coilTaps[]` array of each output's power-line y, and
    `totalCoilRows` driving the rung height, the vertical bus length, and the coil
    hit-target heights. Fixes the "line goes up first" and "bus runs too far down" reports.
- **Popup (modal) parameter editor on element click — done (card v0.23.0).** Selecting an
  element/coil on the canvas (click, or place-then-select) now opens a **modal** (`.modal`
  in `panel.ts`, backdrop + ✕ / backdrop-click to close) instead of the inline inspector
  bar. The body reuses the existing form editors (`_renderSeriesElement`/`_renderCoilEditor`),
  so branches still open the recursive editor. For an **fb** element the modal also embeds
  the referenced instance's **parameters** (`_renderFbInstancePanel`: type select +
  `fbFields` params via `_renderFbParam`), so preset/PV/reset are editable right there —
  no need to visit the separate "Function blocks" section first. Titled per element type
  (`_elementTitle`/`_coilTitle`). The old `.inspector` inline bar is replaced.
  - **Two-click open (card v0.24.1).** The popup no longer opens on the first click: a
    first click **selects** (outline), a second click of the already-selected element opens
    the popup (new `_modal` state gates `_renderInspector`; `_selectEl`/`_selectCoil` set
    `_modal=true` only when the click repeats the current selection). Placement (place-then-
    select, drag-drop, add-path) selects the new element with the popup **closed**, so you
    get the element selected instead of an immediate popup. Closing the popup keeps the
    selection (click again to reopen). Requested 2026-07-18.

**Editor-UX polish (card; small–medium — recorded 2026-07-17).**
- **OR wraps the selected element — done (card v0.24.0; two-path v0.24.3).** Pure
  `elements.ts` `wrapInBranch(program, ni, ri, steps, ei)` replaces the element at that
  position with an OR branch `[[thatElement], []]` — the element in path 1 and a second
  **empty** path ready to fill (that empty path is what makes it a real OR; validation flags
  it until filled). A branch is left unchanged.
  Surfaced two ways: a **"Wrap OR"** button in the element's action row (`_elActions`, shown
  in the popup and structure editor), and — since the two-click popup (v0.24.1) means a
  first click only *selects* — **pressing the OR palette tool while an element is selected**
  wraps it (`_maybeWrapSelection` in `_placeUp`: a press-release of the OR/branch tool with a
  non-branch element selected wraps instead of arming). Unit-tested (`test/elements.test.ts`).
- **Bigger, square palette buttons — done (card v0.23.1; Select squared in v0.24.0).** All
  palette buttons — the draggable tools *and* Select — are now 46×46 squares
  (`.palette button.chip`). The canvas-style glyphs remain a later *stretch*. In v0.23.1 the
  destructive **✕ delete** icon buttons also became **red by default** (`button.icon`), with
  a `neutral` variant for non-destructive icons (modal close, reorder ↑/↓), so delete reads
  clearly different from the popup's close ✕.
- **Red background on the erroring position — done (card v0.24.0).** `validate.ts` issues
  now carry the flagged position (`steps`+`ei` for a series element, `ci` for an output;
  rung-level warnings carry none). `render.ts` draws a translucent-red `err-cell` rect over
  each flagged element/output cell (via `CanvasEdit.errorEls`/`errorCoils`, pointer-
  transparent so it never blocks a click; guarded so the read-only card is unaffected). The
  panel feeds the positions from `validateProgram` per network in `_editConfig`. The symbol
  shows through the tint; the validation bar still lists the details.

**Editor layout & workflow (card; recorded 2026-07-18).** A batch of
editor-page ergonomics, all card-side (`panel.ts` + CSS, a couple touch render/layout).
Grouped roughly in the order they'd logically be tackled:
- **Split-scroll layout — done (card v0.31.0; toolbar-pin fix v0.32.0).** `:host`/`.body` are
  now a flex column; the pinned `.edit-top` section holds validation + tag list + FB list +
  palette/controls, and the networks live in a separate `.canvas-scroll` (`flex:1; overflow:auto`)
  below. `_renderCanvas` was split into `_renderCanvasControls()` (pinned) +
  `_renderCanvasNetworks()` (scrolls; resets `_geom`); the inspector modal moved up to `render()`.
  **v0.32.0:** `.edit-top` no longer scrolls as a whole (was `max-height:60%` internal scroll,
  which pushed the palette below the fold with a long tag list); instead each list body caps its
  own height (`.list-body { max-height:30vh; overflow-y:auto }`), so the palette/toolbar stays
  **always visible** above the scrolling ladder. **v0.32.1:** the pinned region is split into two
  visually-distinct blocks — `.define-block` (tags + FBs) and `.toolbar-block` (validation +
  palette/zoom/actions) — above the `.canvas-scroll` block, so the page reads as three blocks:
  define · toolbar · canvas.
- **Remove the left live-preview pane — done (card v0.31.0).** The two-column grid + `.preview`
  section + `_renderPreview` are gone (redundant with the live editor canvas); the editor is now
  full-width single-column.
- **Collapsible Tag list and FB list — done (card v0.31.0).** Each list header is a `button.collapse`
  (chevron + title + count) toggling `_tagsOpen`/`_fbsOpen` (state, persists across re-renders);
  the table/rows render only when open.
- **FB list as a table — done (card v0.33.0).** `_renderFbs`/`_renderFbRow` now render a
  `table.tags` with Name / Type / Parameters / delete columns (params in a `.fb-params` flex
  cell), matching the tag table.
- **FB placement without pre-declaring — done (card v0.33.0).** The palette **FB tool is always
  present** (was hidden with no instance). Placing it (click a slot or drag-drop) routes through
  `_elementFor(tool, program)`, which for the FB tool **auto-creates a fresh instance** (`addFb`,
  default `R_TRIG`) and inserts an `fb` ref to it, then **opens the popup** (`_modal = label==="FB"`)
  so the new instance's type + params are configured inline (`_renderFbInstancePanel`). The
  structure editor's "+ FB" (`_addFbElement`) does the same. A function block stays top-level-only:
  the FB tool is excluded from `allowNestedInsert` and a hard guard drops a nested FB drop.
- **Rename the `coil` tag kind to "output" in the UI — done (card v0.31.0), *tag list only*.**
  `TAG_KIND_LABELS` maps `coil`→"output" for the tag-kind `<select>` option text only; the IR/
  DSL/schema kind stays `coil` and the rung-*output* coil element is unchanged. "output" fits
  better in the tag list because such a tag can also be **REAL** (not only a boolean coil).
- **Canvas zoom presets (small/medium/large) — done (card v0.32.0).** A `_zoom` factor
  (`ZOOM_PRESETS` 0.7/1/1.4) sizes each network SVG in **pixels** (`width=VIEW_WIDTH*zoom`,
  `height=rendered.height*zoom`) instead of `width=100%`; `.canvas-scroll` scrolls if the
  ladder overflows. A segmented Small/Medium/Large control sits in the pinned canvas header.
  No pointer-math change was needed after all — `_toUserXY` uses `getScreenCTM().inverse()`,
  which already reflects the real on-screen scale, so hit-testing stays correct at any zoom.

## Editor feedback round (recorded 2026-07-19, from first real authoring use) — not started

The user's first proper program-building session. Nothing here is built yet; more items may
follow. Ordered so the two **regressions/bugs** come first — they block or confuse things that
used to work — then the canvas-interaction wishes, then rendering.

**Bugs / regressions (do these first).**
- **Tag dropdowns no longer offer `instance.ET` — regression.** Since tag selection became a
  `<select>` (4.2/4.3), a compare operand can no longer be set to a function-block output like
  `t1.ET`; it is only reachable via *edit as text*. The IR and backend already accept dotted
  operands (int v0.6.0) — the UI just never lists them. **Fix:** `_tagSelect` (or a variant) must
  include the available `instance.<OUTPUT>` refs alongside tag names, wherever a dotted ref is
  legal (compare operands today; contacts too once the item below lands). *Feasibility: high.*
- **Rung titles are not drawn; the network title appears twice.** On the canvas the user sees the
  network name repeated a second time, slightly smaller, and never sees rung titles. Cause: the
  panel's DOM `.cv-net-head` shows `net.id` + `net.title` *and* `renderNetwork` draws
  `network.title` inside the SVG, while `renderRung` draws no rung title at all. **Fix:** render
  the **rung** title per rung and drop the duplicate network title (keep one of the two).
  *Feasibility: high.*

**Function-block pins as first-class references (backend + card; the big one).**
- **Read a block's BOOL outputs — e.g. `TON.Q` on a contact.** Today only *numeric* fb outputs
  (`ET`/`CV`) are injected into the scan values and only a `compare` operand may reference them.
  Extend that to boolean outputs so a contact can reference `instance.Q` directly, instead of
  routing the block's power through the rung. Touches `scan.py` (inject bool outputs alongside
  `ET`/`CV`), `model.py` validation (`_fb_numeric_outputs` → a general per-type output table),
  the schema, the DSL, and the card (contact editor + `power-flow`). *Feasibility: medium.*
- **Write a block's inputs — e.g. `SR.R` as a coil target.** Fundamentally different from the
  above: this drives a block *input* from a rung, whereas today secondary inputs are bound to a
  **tag** in the instance declaration (option A, int v0.7.0). Allowing `( SR.R )` as an output
  target would be a second, parallel way to wire the same pin. **Needs a design decision before
  implementing** — either allow it and define precedence when both the declaration binding and a
  coil write exist, or keep the tag-binding model and just make the declaration easier to fill in
  from the popup. *Feasibility: medium, but decide the model first.*

**Clock / calendar in the program (backend + card) — requested 2026-07-19.**
Make the current time available so a rung can compare against it (e.g. "after 22:30 on a
weekday"). User's proposal: a function-block instance exposing `h` (24h), `m`, `s`, `weekday`
(1–7 = Mon–Sun), optionally date fields. **This fits the existing machinery well:**
- `evaluate(program, image, now, previous)` already receives a full `datetime` (`scan.py`
  `now: datetime | None`) — the engine stays pure, nothing new is injected.
- Numeric fb outputs already have a complete path (int v0.6.0): `_solve_rung` injects
  `instance.<OUT>` into the scan `values`, the coordinator merges them into `state_image`, a
  `compare` operand may reference the dotted name, the DSL round-trips it, and validation
  allows it via `_fb_numeric_outputs`. A clock block's outputs ride that same path, so
  `[ clock.H >= 22 ]` works with no new IR concept.

*Decisions to settle before implementing:*
- **Do not call the type `TIME`** — `TIME` is already a *tag type* (`BOOL`/`REAL`/`TIME`), so
  `fb clock = TIME` would be genuinely confusing. Prefer **`CLOCK`** (or `RTC`).
- **Output naming/case.** Existing pins are uppercase (`Q`, `ET`, `CV`) and the DSL's dotted-ref
  regex is case-sensitive, so use `H`, `M`, `S`, `WD` rather than `h`/`m`/`s`/`weekday`.
- **Add "minutes since midnight"** (e.g. `TOD`, 0–1439). Comparing `H` and `M` separately makes
  a window like 22:30–06:15 awkward (`H>22 OR (H==22 AND M>=30)`); `TOD >= 1350` is one compare.
  Strongly recommended — likely the most-used output.
- **Local time, not UTC.** The coordinator currently passes `dt_util.utcnow()`, so `now.hour`
  would be UTC. Switch it to `dt_util.now()` (tz-aware **local**). This does **not** affect
  timers: they use `now.timestamp()`, which is the same absolute epoch either way, and stays
  continuous across DST. Check the tests for any `utcnow` assumption.
- **Must the block be placed in a rung?** Today `instance.<OUT>` is only injected *after the
  block is solved in a rung*, so a declared-but-unplaced instance exposes nothing. A clock has
  no rung input and no state — the natural fit is to **solve `CLOCK` instances once at the start
  of each scan** (which also matches "read at the start of the cycle") so its outputs are usable
  without placing an `fb` element. If placed anyway, define `Q` (simplest: pass the rung power
  through / always conduct).
- Weekday 1–7 = Mon–Sun is exactly Python's `isoweekday()`. Date extras (`D`, `MO`, `Y`) are
  cheap to add on the same block once the above is settled.

**Canvas interaction (card).**
- **Reorder rungs by dragging** within a network (works today only via the structure editor's
  ↑/↓). The pure `moveRung` helper already exists; this is the canvas gesture — a drag handle or
  a draggable rung header — plus a drop indicator between rungs. *Feasibility: medium.*
- **Drag elements between rungs** in the same network. Already noted as out of scope for stage C
  (a reorder drag resolves within the *source* rung); the user now confirms it is wanted. Needs
  `hitRung` consulted during a reorder drag and `moveElementAcross` generalised to a target rung.
  *Feasibility: medium.*
- **Drag coils/outputs.** Outputs are append-only today (a coil drag appends to the rung it lands
  on) and can only be reordered from the inspector. Wanted: drag an output to reposition it in the
  coil stack, like series elements. *Feasibility: medium.*
- **Duplicate an element by Ctrl-dragging it.** `PointerEvent.ctrlKey` is available on
  move/up, so on drop with Ctrl held, insert a deep copy instead of moving the original.
  *Feasibility: medium.* **Open question:** duplicating an `fb` element — should it reference the
  *same* instance (two contacts on one timer) or auto-create a fresh instance? Probably a fresh
  instance, matching the v0.33.0 place-an-FB behaviour; decide when implementing.
- **Edit network and rung names on the canvas** (today only via the structure editor). Inline
  editable title fields in the network/rung headers. *Feasibility: high.* Pairs naturally with
  the rung-title rendering fix above.

**Rendering.**
- **Compare block layout.** Put the **operator** inside the box and show the **value/tag being
  compared against below** the box (like the fb/output blocks show their operands). Today the box
  renders the whole `left OP right` expression inline. `render.ts` `drawCompare` +
  `layout.ts` measurement if it needs more rows. *Feasibility: high.*

**Docs — both repos' `README.md` rewritten (2026-07-19) — done.** Status is now **beta**; both
lead with "this is not a PLC and you don't need one — it's a way of programming HA automations",
state that the integration *and* the card repo are both required, and cover install via HACS.
The integration README documents creating a service/program and editing it in the sidebar panel
(tag kinds, palette elements, click/drag, Save); the card README covers the editor panel plus
adding the **read-only** status card to a dashboard. Copyright holder in both `LICENSE` files
(and the card's `package.json` author) is now **Hermeler Engineering**, not a personal name.

**Agreed next-up order:** the 2026-07-17 visual round (TIA parameter display + popup editor)
is **done** (card v0.21.0–v0.24.x, v0.30.0). The **editor layout & workflow batch is complete**
(card v0.31.0–v0.33.0): split-scroll + three-block layout, preview removed, collapsible +
tabular tag/FB lists, coil→"output" label, canvas zoom presets, and FB placement without
pre-declaring. Timer durations with units + the per-type FB palette landed in card v0.34.0.
The READMEs were refreshed 2026-07-19.

**Next up:** the **editor feedback round (2026-07-19)** above — the user is now building real
programs and will likely add more items. Start with its two **regressions/bugs** (fb outputs
missing from the tag dropdowns; rung titles not drawn / network title duplicated), which are
both small and unblock authoring. **Still parked:** phase **4.5** (validation UX + YAML
export/import polish) and canvas-style palette glyphs.

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
