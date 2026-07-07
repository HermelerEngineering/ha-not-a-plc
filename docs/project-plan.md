# Ladder-style Logic Engine for Home Assistant — Project Plan

*Name: **"Not a PLC"** (domain `not_a_plc`) — decided 2026-07-07. "Ladder" now refers only to the logic paradigm (ladder logic, rungs, coils), never to the product.*

> All code, DSL keys, UI strings and documentation are written in English so the project can grow into an official HACS integration if it gains wider interest. Other languages are added later through Home Assistant's translation layer, not by translating the source.

## 1. What it is, and what it deliberately is not

A native Home Assistant custom integration (installable through HACS) that runs a **cyclic logic engine**: roughly every 500 ms (configurable, slower is fine) it reads a set of HA entities into an internal process image, evaluates a program built from ladder-style networks, and publishes the results as **real HA entities**. Alongside it, a graphical status view ("online monitoring"), and eventually a graphical editor where you drag the logic together instead of typing it.

Deliberately not:

- Not a PLC and no pretence of real time. No deterministic scheduler, no guarantees below ~100 ms.
- No Modbus bridge, no sidecar container, no external runtime. Everything runs inside the HA event loop.
- Not a full IEC 61131-3 implementation. We take only the subset that is useful at home.

The value we are after is the **way of working** (cyclic, with retentive state and explicit networks) plus **fully native integration**: coils and memory bits are just entities — visible on dashboards, usable in other automations, present in history and the recorder.

## 2. Architecture in layers

Five layers, inside out:

1. **Engine (headless).** Pure Python, no HA dependencies in the core. Takes a program model plus a process image (dict of tag → value) and returns a new process image. Fully unit-testable without a running HA.
2. **Integration / entity layer.** Config flow, coordinator (the cyclic driver), and the platforms that publish coils/memory bits as entities. **Multiple instances:** each Not a PLC "service" is its own config entry — its own device, program, entities and scan loop — so you can run several independent programs side by side (see §9).
3. **Websocket API.** The bridge to the frontend: `get_program`, `save_program`, and a `subscribe_state` that pushes the full process image after each scan. Both the monitor card and, later, the editor hang off this. With multiple instances, every command targets a specific service by its `entry_id`.
4. **Frontend.** Two deliverables from one repo, sharing the render/power-flow layer: a **read-only status card** (SVG, stays as-is) and, later, a **full-page editor panel** reached from the HA menu (like ESPHome Builder). The card is not replaced by the editor — they coexist.
5. **Program representation (IR).** The serialisable model that flows through all layers — the linchpin of the whole plan (see §3).

The split between layer 1 and the rest matters most: by keeping the engine a pure function (`(program, process_image) → process_image`), you test it with snapshots and it stays independent of HA versions.

## 3. The program representation (IR) — the linchpin

This is the key decision that makes the graphical editor "free". There is a single canonical, serialisable program model (JSON). Both the text DSL and the graphical editor are nothing more than producers/consumers of that model. By fixing the schema in phase 0, the editor later builds on exactly what the engine already executes — it is not bolted on afterwards.

**Canonical form = element graph, not an expression tree.** An expression (`A AND (B OR C)`) evaluates nicely but draws badly. The ladder graph both draws and evaluates cleanly. A rung is a **series chain** of positions; each position is either a single element (contact) or a **parallel branch** (list of sub-chains = OR), and the chain ends in one or more coils. This is exactly the classic ladder structure.

Schema sketch (phase 1, bit logic only):

```jsonc
{
  "meta": { "version": 1, "name": "Ventilation" },
  "scan_interval_ms": 500,

  "tags": {
    "presence":   { "kind": "input",  "source": "binary_sensor.wc_presence",
                    "type": "BOOL", "on_unavailable": "false" },
    "humid":      { "kind": "memory", "type": "BOOL", "retain": true },
    "fan_high":   { "kind": "coil",   "type": "BOOL",
                    "writes": { "target": "switch.fan_high" } }
  },

  "networks": [
    {
      "id": "n1", "title": "Fan high",
      "rungs": [
        {
          "id": "r1",
          "series": [
            { "type": "contact", "tag": "humid",    "mode": "NO" },
            { "branch": [
                [ { "type": "contact", "tag": "presence", "mode": "NO" } ]
            ] }
          ],
          "coils": [ { "type": "coil", "tag": "fan_high", "mode": "=" } ]
        }
      ]
    }
  ]
}
```

**Tag model.** Four kinds, symbolically bound (no `%IX` addresses):

- `input` — reads an HA entity into the process image. Holds the `on_unavailable` policy.
- `coil` — published as an entity; optionally a `writes` binding that calls a service on a real target when it changes (the "executor").
- `memory` — internal bit/value that persists across scans (retentive). `retain: true` additionally survives a restart (persisted to `.storage`). A non-retained `memory` tag is the "static" variable.
- `temp` — scratch value, re-initialised every scan. Names an intermediate result without adding to the retained state; never published, never persisted.

Types stay `BOOL` / `REAL` / `TIME`. (Decision locked, see §9.)

**Storage.** Canonical as JSON in HA's `.storage` (that is where the editor writes). In addition, round-trip import/export to a readable YAML/text DSL, so you can keep the program under version control in Forgejo and review it with TortoiseGit/VS Code. The DSL is a 1-to-1 serialisation of the same graph, not a separate language — so round-trips are lossless.

## 3a. Entity binding from the editor

Network inputs and outputs are chosen in the editor directly from the devices and entities already present in HA — no typing entity IDs. HA ships the building blocks for this to the frontend, so this is UX work, not engine work.

- **Native pickers.** The editor uses the built-in `ha-device-picker` and `ha-entity-picker` (or an `entity` selector). You get the same search/filter experience as everywhere in HA, including grouping by device and area. Flow: pick a device first, then the entity within it — or search on entity directly.
- **Domain filter per role.** For an `input` tag the picker filters to readable state entities (`sensor`, `binary_sensor`, `input_boolean`, …). For a coil `writes` target it filters to controllable domains (`switch`, `light`, `fan`, `input_boolean`, …). That way you never offer an impossible choice.
- **Type inference.** Based on the chosen entity the editor sets the tag type automatically: `binary_sensor`/`input_boolean` → `BOOL`, a numeric `sensor` → `REAL`. Manually overridable, but the default is almost always right.
- **IR stays entity-id.** The source of truth in the model remains the bare `entity_id` (`source` on inputs, `writes.target` on coils) — that is what the engine reads, and it keeps the git diff clean. Device and friendly names are resolved live from the registries for display; they are not denormalised into the IR.
- **Rename robustness.** Optionally we store the registry `unique_id` alongside `entity_id`, purely to detect on load that an entity was renamed or removed and warn the user — the engine itself keeps running on `entity_id`.

The registries (entity/device/area) are available through the websocket API; by using the built-in selectors you get search, filtering and area grouping for free instead of building it yourself.

## 4. The scan cycle

Each tick, in three phases:

1. **Snapshot.** Read all `input` tags from HA into an internal process image and freeze it. `unavailable` is handled per tag according to `on_unavailable` (`false` / `hold`). This is your equivalent of an input fault.
2. **Solve.** Evaluate the networks deterministically top-down. Each rung is solved against the frozen image; memory bits and coil states are updated in the image. If two rungs write the same coil, the last one wins — just like a real scan.
3. **Write-on-change.** Publish coil/memory entities, and run `writes` bindings only on an actual change. No service calls every half second.

**Clock injection.** The scan takes the current time as an argument (`(program, image, now) → image`) rather than reading the wall clock inside blocks. This keeps the engine pure and, crucially, makes timers testable with a fake clock (see §7).

**Retention:** `retain` memory bits and coils restore their value after a restart via `RestoreEntity`/`Store`. **Diagnostics:** measure the scan time per cycle and warn on overrun (scan takes longer than the interval) — useful as the program grows.

## 5. Phased plan

### Phase 0 — Foundation (schema-first)

Goal: a skeleton that runs end-to-end with one trivial rung.

- Repo scaffolding as a HACS integration: `manifest.json`, `config_flow.py`, an empty coordinator with a 500 ms tick.
- Definition of the IR schema (§3) including JSON-schema validation.
- One hard-coded dummy rung that drives a single coil entity.

Done when: you see a coil `binary_sensor` toggle based on one input, purely via the tick.

### Phase 1 — Bit-logic engine

Goal: full bit logic, definable in text.

- Elements: contact NO/NC; coils `=`, `S` (set/latch), `R` (reset); series (AND), parallel branch (OR), `NOT`.
- Tag binding for `input`/`coil`/`memory`, with the `writes` executor and `on_unavailable` policy.
- Full scan cycle with write-on-change and retention.
- Text DSL ↔ IR round-trip (import/export).

Done when: the humidity-hysteresis case (two comparators → one latch → coil) runs fully in bit logic, survives a restart, and is green in the tests.

### Phase 2 — Graphical status view — DONE (shipped, live in HA)

Goal: the "online monitoring" feel, read-only. This builds the render pipeline the editor reuses later.

- Websocket API: `get_program` + `subscribe_state` (process image after each scan). ✅
- Custom Lovelace card (Lit/TypeScript, SVG): draw the rungs from the IR, colour "energised" elements based on live state. At 500 ms this looks smooth enough. ✅ (energised uses the theme's `--state-active-color`, amber in the default dark theme, not literal green.)
- Read-only; no editing. ✅

Done: the program "flows" live in a dashboard card; both repos ship as HACS custom repositories (`v0.0.1`). Phase 2 is frozen — new status-view work lands in phase 2A, not here.

### Phase 2A — Status-view polish & multiple services (next up)

Goal: finish the monitoring experience and let one HA run several independent
programs — all managed from the UI. Pick this up before phase 3.

**Everything via the UI — no YAML/JSON.** The user configures services and picks
what to view entirely through the HA interface; hand-editing YAML/JSON is never
required. (Program *editing* itself is the phase-4 editor; here it's service
lifecycle + the card.)

- **Multiple services (config entry per instance).** Relax the single-instance
  config flow so *Add integration → Not a PLC* creates another service, each with
  its own device, program, entities and scan loop. Name each service in the flow.
  Soft cap with a scan-load warning (tie to the §4 scan-time diagnostics), no hard
  limit. The websocket API and card must target a service by `entry_id` (they
  resolve "the single instance" today). See §9.
  - *Dependency:* a per-service program only becomes meaningful once programs are
    user-owned (currently every entry loads the bundled `demo.json`). Sequence this
    with the editable-program-in-`.storage` work; until then services share the
    demo program.
- **Card: service selector.** A card option to choose which service (`entry_id`) it
  renders, so each dashboard card can show a different program.
- **Card visual polish (concrete fixes from live use):**
  - **Larger text.** Tag/mode/coil labels are hard to read — bump the font sizes.
  - **Align the left rail.** The energised left stub must connect to the network's
    vertical rail (the white line); today there is a gap so the coloured stub does
    not meet the rail.
  - **Coils as `( )`.** Draw coils as a parenthesis pair `( )` (as in common PLC
    packages), not a circle. `S`/`R` shown inside as `(S)` / `(R)`.
- **Liveness heartbeat.** A small dot in the top-right corner that toggles green on
  every scan (driven by each `subscribe_state` push), so you can see at a glance
  that the engine is cycling; a stalled/failed scan shows as the dot stopping.

Done when: you can create a second Not a PLC service from the UI, point a card at
it via the selector, and the card reads clearly with rail-aligned rungs, `( )`
coils and a blinking heartbeat.

### Phase 3 — Extended function blocks

Goal: from bit logic to real control blocks. Function blocks carry state → separate instance declaration in the IR (`fbs`), with retention where meaningful.

- Edge detection: `R_TRIG`, `F_TRIG`.
- Timers: `TON`, `TOF`, `TP` — counting on wall-clock delta per cycle (via the injected clock), not on scan counts.
- Counters: `CTU`, `CTD` (`CTUD` optional).
- Comparators: `GT/GE/LT/LE/EQ/NE` on `REAL` — the bridge to analog sensors (humidity, temperature, lux).
- Latch `SR`/`RS` as explicit blocks.

Done when: the ventilation case with a 15-minute run-on (`TOF`) and hysteresis via comparators runs fully as blocks, including the live view.

### Phase 4 — Graphical editor

Goal: build logic by dragging; serialises to the same IR the engine executes.

**Delivery — a full-page HA panel, not a card.** The editor is its own menu item
(a custom panel, ESPHome-Builder style), registered via `panel_custom`. It reuses
the phase-2 render/power-flow layer and ships from the same frontend repo as the
card. The read-only status card stays exactly as it is, alongside the editor.

**Screen layout:**

- **Tag panel** (top): create and manage the program's tags — `input`, `coil`
  (output), `memory`, `temp` (see §3 tag model) — binding inputs/outputs via the
  native HA device/entity pickers with domain filter and type inference (see §3a).
- **Element toolbar** (below the tag panel): the available elements (contact NO/NC,
  coil `=`/`S`/`R`, parallel branch, `NOT`, and later the phase-3 function blocks),
  dragged from here onto the work area.
- **Work area**: a grid canvas holding the networks; drop elements onto the grid
  and wire them into rungs.
- **Scan-interval presets**: a selector for `500 ms`, `1 s`, `2 s`, `5 s`, `10 s`;
  add `250 ms` and `100 ms` only when the scan-time diagnostics (§4) show the
  instance has headroom at that rate.
- **Service selector**: with multiple instances (§9), the editor edits one service
  (config entry) at a time and lets you switch between them.

**Behaviour:**

- Validation: dangling wires, unbound tags, warning on multiply-written coils.
- Save via `save_program` (canonical JSON in `.storage`); export to YAML for git.

Done when: you build a complete network graphically, save it, and it runs without a
line of hand-written YAML.

## 6. Repo and package structure

```
custom_components/not_a_plc/
  __init__.py            # setup, coordinator start
  manifest.json
  config_flow.py
  coordinator.py         # cyclic tick + scan orchestration
  engine/
    model.py             # IR data structures + schema validation
    parser.py            # DSL <-> IR round-trip
    scan.py              # snapshot -> solve -> write-on-change
    blocks.py            # (phase 3) TON/TOF/CTU/... as state objects
  binary_sensor.py       # coils/memory as state mirror
  switch.py              # (option) commandable/overridable coils
  websocket_api.py       # get_program / save_program / subscribe_state
  strings.json           # base UI strings (English)
  translations/en.json

# Separate repo for the frontend (Lit/TS, HACS "Dashboard"):
ha-not-a-plc-card/
  src/
    ir.ts                # TS mirror of the IR
    power-flow.ts        # pure (IR, state) -> energised; shared by card + editor
    render.ts            # SVG ladder rendering; shared by card + editor
    ladder-card.ts       # read-only status card (custom:not-a-plc-card)
    editor-panel.ts      # (phase 4) full-page editor panel (panel_custom)
```

Two HACS items: the integration and the frontend. The frontend repo ships **both**
the read-only status card and (phase 4) the full-page editor panel, sharing the
render and power-flow layer so the editor draws exactly what the engine executes.

## 7. Testing strategy per phase

The guiding split: the **engine is a pure function**, so it is unit-tested without HA (fast, table-driven). The **integration layer** (entities, config flow, coordinator, websocket) is tested with `pytest-homeassistant-custom-component`, which provides HA's own test fixtures (`hass`, `MockConfigEntry`, entity/device registries, a websocket client, and a time `freezer`). The **frontend** (card/editor) is tested separately in TypeScript (e.g. `vitest`).

The single most valuable test asset is a **golden-program corpus**: a set of example programs (including the ventilation case) each with a recorded input→output trace. Running these in CI is what protects behaviour across refactors and across phase boundaries. Build it in phase 1 and extend it every phase.

**Phase 0 — Foundation**
- Config-flow test with the `hass` fixture: an entry can be created and reconfigured.
- Coordinator tick test: advance the clock with `async_fire_time_changed` and assert the tick fires.
- Schema-validation tests: valid IR passes; malformed IR is rejected with a clear error.
- One end-to-end smoke test: minimal program, one input entity → one coil entity toggles.

**Phase 1 — Bit logic**
- Pure engine unit tests, table-driven: `(program, input_snapshot) → expected outputs`, covering NO/NC contacts, series (AND), parallel (OR), NOT, and coils `=`/`S`/`R`.
- Multi-cycle tests: S/R latch state persists across scans; last-write-wins on a duplicated coil.
- Round-trip invariant: IR → DSL → IR equals the original (serialisation fidelity).
- Optional property-based tests (`hypothesis`): e.g. a rung with no true path yields a false coil for `=`.
- Integration tests with `hass`: an input entity change, then a tick, produces the coil entity change and the `writes` service call (assert via `async_mock_service`). Test `on_unavailable` (`false` vs `hold`) by marking the entity unavailable.
- Retention test: reload the entry ("restart") and assert a retained memory bit persists.

**Phase 2 — Graphical status view**
- Websocket tests with the `hass` ws client: `get_program` returns the IR; `subscribe_state` pushes an update after a scan.
- Frontend: separate the "compute power flow" logic (`(IR, state) → which elements are energised`) from the SVG DOM, and unit-test that pure function. Add SVG snapshot tests for a few fixture states.
- Visual acceptance: load a known program, drive inputs, eyeball the green flow.

**Phase 3 — Extended function blocks**
- Timers are the tricky part; the injected clock (§4) makes them deterministic. Pure tests advance a fake clock and assert `Q` transitions exactly at the boundary, including reset and partial-scan timing, for `TON`/`TOF`/`TP`.
- Counters: `CTU`/`CTD` edge counting, reset dominance, limits.
- Edge detect: `R_TRIG`/`F_TRIG` produce a single-scan pulse and do not re-fire without an intervening opposite edge.
- Comparators: boundary values, and behaviour when the `REAL` input is unavailable.
- Integration scenario: drive the 15-minute run-on end-to-end using the `freezer` fixture (freezegun) to jump time, asserting the coil holds then drops.

**Phase 4 — Graphical editor**
- Unit-test the editor's IR (de)serialisation and validation rules (dangling wires, unbound tags, duplicate-coil warning) as pure TS functions.
- Golden round-trip that ties frontend and backend: an editor-produced IR is saved, reloaded by the engine, and executes identically to its recorded trace.
- Picker helpers: test the domain-filter predicate and the type-inference function as pure units; trust the native HA pickers themselves.
- Optional heavier end-to-end (Playwright/Cypress against a dev HA): build a rung, save, verify the entity toggles. Good as a later CI smoke test, not required early.

**Commissioning aids (cross-cutting).** A `force`/`override` service to pin a tag temporarily, and a `reload` service to re-read the program without a restart. Both are also handy as test hooks.

## 8. Road to an official HACS integration

Design for publication from the start, even while it is a personal project; retro-fitting these later is painful.

- **Repo standards.** Public repo with description and topics, a `README`, a `LICENSE`, the `custom_components/not_a_plc/` layout, a `manifest.json` with `domain`, `name`, `codeowners`, `documentation`, `issue_tracker`, `version` and `iot_class`, and a `hacs.json` in the root. Ship tagged semantic-version GitHub releases.
- **Brands.** Add the domain and icons to the `home-assistant/brands` repository so the logo shows in HA/HACS — required for default-store inclusion.
- **CI validation.** Run `hassfest` and the `hacs/action` validator in GitHub Actions so the repo stays release-ready; add `ruff` (lint/format) and `mypy` (typing), plus `pytest` with a coverage gate. Frontend: `eslint`, `tsc --noEmit`, `vitest`.
- **UX baseline.** A UI config flow (no YAML setup), `strings.json` + `translations/en.json`, and clear options. This is effectively expected of a quality HACS integration.
- **Quality scale as north star.** Follow Home Assistant's integration quality scale (bronze → silver → gold → platinum) as a checklist: config flow, test coverage, typing, docs, `runtime-data`, graceful unavailable handling. Useful even if we never submit to core.
- **Publication step.** Once the requirements are met, submit a PR to the `hacs/default` list to have the repo added to the default store, so users install it without adding a custom repository.

The exact HACS and brands requirements evolve; verify against the current HACS "publish" docs and the brands/quality-scale docs at the moment we actually publish rather than trusting a snapshot.

## 9. Decisions to lock now

- **Name.** Resolved: product **"Not a PLC"** (domain `not_a_plc`). "Rung" and "Coil" are kept as ladder-paradigm terms.
- **Coil actuation.** Proposal: a coil always publishes its logical truth as a `binary_sensor`, and optionally carries a `writes` binding that calls a service on a real target when it changes. That separates "logic truth" from "actuation" and mirrors your trigger/decision/executor pattern. A `switch` variant (manually overridable) is a nice extra for commissioning, but not the default.
- **Canonical storage.** JSON in `.storage` as the source of truth (editor-friendly) with lossless YAML export — or the other way round? I lean toward JSON canonical.
- **Phase-1 scope.** Only `=`/`S`/`R` plus series/parallel/NOT, or pull `R_TRIG`/`F_TRIG` in already? Strictly you do not need edge detection for S/R latches, so I would keep it in phase 3 and keep phase 1 pure bit.
- **Tag model.** Resolved (2026-07-08): four kinds — `input`, `coil`, `memory`
  (retentive across scans; `retain` adds across-restart), `temp` (scratch, reset
  each scan). "static" = a non-retained `memory` tag. Types stay `BOOL`/`REAL`/`TIME`.
  See §3.
- **Instances.** Resolved (2026-07-08): support **multiple Not a PLC services**,
  one **config entry per service** — each with its own device, program, entities
  and scan loop, **created and configured entirely from the UI (never YAML/JSON)**.
  No hard cap; instead warn when the combined scan load is heavy, tied to the
  per-cycle scan-time diagnostics (§4). This supersedes the current single-instance
  config flow, which must be relaxed to allow multiple entries; the websocket API
  and the frontend must then target a service by `entry_id` (today they resolve
  "the single instance"), and the card gets a **service selector**. First delivered
  in phase 2A (see §5).
- **UI-only, no hand-editing.** The user's standing preference: services, options
  and (via the phase-4 editor) programs are all managed through the HA interface.
  No step in normal use should require editing YAML or JSON by hand.
- **Editor delivery.** Resolved (2026-07-08): the editor is a **full-page HA panel**
  (menu item, ESPHome-Builder style), in the **same frontend repo** as the card,
  reusing the shared render/power-flow layer. The read-only status card stays. See
  §5 phase 4 and §6.
- **Status-card heartbeat.** Resolved (2026-07-08): the status card shows a small
  top-right dot that toggles green each scan (a liveness indicator), driven by the
  `subscribe_state` push. A near-term card follow-up (see §5 phase 2).

---

*Phases 0–2 are complete and live in HA. Next concrete step: phase 3 — extended
function blocks (start with comparators on `REAL`, the bridge to analog sensors).*
