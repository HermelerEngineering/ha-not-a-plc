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
    (canvas.ts) are unit-tested. **Still to do in 4.4:** branch internals interactive
    *inline* (today a branch is one hit-box, edited via the inspector); **stage C**
    pointer-drag to place/reorder; index-based selection can go stale after a
    delete/move (inspector just closes — acceptable for beta). **4.5** validation UX +
    YAML + polish.

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
- **Double custom-element registration (card repo) — still open.** With the panel installed, the
  bundle loads twice (panel `module_url` + the Lovelace card resource), and the Lit
  `@customElement` decorator calls `customElements.define` unconditionally →
  `Failed to execute 'define'… "not-a-plc-card" has already been used`. Fix: guard
  each define so the bundle is idempotent when loaded more than once — e.g. replace
  `@customElement("x")` with a manual `if (!customElements.get("x")) customElements.define("x", C)`
  (or a small `defineOnce` helper) for `not-a-plc-card`, `not-a-plc-card-editor`,
  and `not-a-plc-panel`. Harmless (the first define wins) but noisy in the log.
- **User feedback from the phase-2A/3 round — all addressed.** fb output colouring
  (card v0.3.4), ET no longer floods the websocket (v0.7.4), compares/counters show
  their live value, the editor preview is live (v0.4.0) and the tag table is editable
  (v0.4.1) with full tag management (v0.5.0). Nothing outstanding from that round;
  the only open known issue is the double-define log noise above.

Carried over (fold into a later phase):

- **New brand icons** (`custom_components/not_a_plc/brand/{icon,icon@2x,logo}.png`)
  were replaced by the user 2026-07-13 but are **uncommitted**; they need a commit
  and a new integration release before HACS shows them. (Note `logo.png` is currently
  identical to `icon@2x.png` — HACS brands ideally wants a wider logo, minor.)
- A commandable `switch` coil variant for commissioning is still optional.
- The double custom-element `define` log noise (above) — a quick `defineOnce` guard
  when convenient; harmless.

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
