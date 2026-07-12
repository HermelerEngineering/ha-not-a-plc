# Not-a-PLC — a cyclic ladder-style logic engine for Home Assistant

A native Home Assistant custom integration that runs a **cyclic logic engine**:
on a fixed cycle (default 500 ms) it reads a set of HA entities, evaluates a
program built from ladder-style networks, and publishes the results as **real HA
entities** (visible on dashboards, usable in automations, kept in history).

It brings the *way of working* of ladder programming — cyclic scan, explicit
networks, retentive state — to Home Assistant, with **full native integration**:
no Modbus bridge, no sidecar container, no external runtime.

> This is **not a PLC** and makes no real-time claims. It targets home-automation
> timescales (hundreds of milliseconds), not deterministic industrial control.

## Status: phase 1 (bit logic)

What works today:

- A pure, Home-Assistant-independent **engine** (`custom_components/not_a_plc/engine/`):
  contacts (NO/NC), series (AND), parallel branches (OR), `NOT` groups, and coils
  `=` / `S` (set) / `R` (reset), with a validated JSON program model (the "IR").
- **Retentive state**: `S`/`R` latches carry across scans; `retain: true` memory
  bits survive a restart (persisted to `.storage`, restored before the first scan).
- A **coordinator** that runs the scan cycle (snapshot → solve → write-on-change)
  on a `DataUpdateCoordinator`, with per-tag input interpretation (`true_states`)
  and an `on_unavailable: false | hold` policy backed by input history.
- Coils/memory bits published as `binary_sensor` entities under one "Not-a-PLC" device.
- A lossless **text DSL** ↔ IR round-trip so programs can live in git.
- A UI **config flow** (single instance) and a self-contained **demo program**
  (`programs/demo.json`) whose coil follows the sun — so it works out of the box
  without any user entities.

See [`docs/project-plan.md`](docs/project-plan.md) for the full phased plan
(bit logic → graphical status view → function blocks → graphical editor) and the
route toward becoming an official HACS integration.

## Install via HACS (custom repository)

This repo is a standard HACS custom-integration layout (`custom_components/not_a_plc`).

1. In Home Assistant: **HACS → ⋮ (top right) → Custom repositories**.
2. Repository: `https://github.com/HermelerEngineering/ha-not-a-plc`
   — Category: **Integration**. Add it.
3. Open the new **Not-a-PLC** entry and **Download** it.
4. **Restart Home Assistant.**
5. Add the integration via *Settings → Devices & Services → Add integration → Not-a-PLC*.
6. You should get a `binary_sensor.not_a_plc_daylight` that is `on` during the day.

For the live status view, also install the companion
[**Not-a-PLC Card**](https://github.com/HermelerEngineering/ha-not-a-plc-card)
(HACS category *Dashboard*).

Prefer a copy-in install? Copy `custom_components/not_a_plc` into your Home
Assistant `config/custom_components/` directory and restart.

## Develop

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

ruff check . && ruff format --check .
mypy custom_components/not_a_plc
pytest -q
```

The engine is pure standard library, so its tests run fast and without Home
Assistant; the integration tests use `pytest-homeassistant-custom-component`.

## License

MIT — see [LICENSE](LICENSE).
