# Not-a-PLC — ladder-style logic for Home Assistant

**This is not a PLC — and you do not need one.** Nothing here talks to industrial
hardware, and no PLC, Modbus bridge, sidecar container or external runtime is
involved. Everything runs inside Home Assistant.

What it *is*: a different **way of programming your automations**. Instead of
trigger-based automations, Not-a-PLC runs a **cyclic scan** — on a fixed cycle
(default 500 ms) it reads your entities into a frozen snapshot, solves a program
built from ladder networks, and writes the results back as **real HA entities**
(visible on dashboards, usable in automations, kept in history).

If you have ever written ladder logic, this will feel familiar: contacts, coils,
latches, timers, counters, edge detection — drawn as rungs, evaluated top-down,
every cycle. If you have not, it is simply a very visual and predictable way to
express "when these conditions hold, do this".

> **No real-time claims.** It targets home-automation timescales (hundreds of
> milliseconds), not deterministic industrial control. Do not use it for anything
> safety-critical.

## Status: beta

Not-a-PLC is in **beta testing**. It is usable day to day, but expect rough edges
and occasional breaking changes to the program format while things settle.
Feedback and issue reports are very welcome.

## You need both parts

Not-a-PLC ships as **two HACS repositories**, and you need both:

| Part | Repository | HACS category | What it does |
|------|-----------|---------------|--------------|
| **Integration** | [`ha-not-a-plc`](https://github.com/HermelerEngineering/ha-not-a-plc) (this repo) | Integration | Runs the logic engine and publishes the entities |
| **Card / editor** | [`ha-not-a-plc-card`](https://github.com/HermelerEngineering/ha-not-a-plc-card) | Dashboard | The **graphical editor panel** and a read-only status card |

The integration is the engine; the card repository provides the **editor** you
build your programs in. Without it you would have to edit the program as text, so
install both.

## Install

Install **both** repositories as HACS custom repositories:

1. In Home Assistant: **HACS → ⋮ (top right) → Custom repositories**.
2. Add `https://github.com/HermelerEngineering/ha-not-a-plc` — category **Integration**.
3. Add `https://github.com/HermelerEngineering/ha-not-a-plc-card` — category **Dashboard**.
4. Open each new entry and **Download** it.
5. **Restart Home Assistant.**

Prefer a copy-in install of the integration? Copy `custom_components/not_a_plc`
into your Home Assistant `config/custom_components/` directory and restart.

## Creating a program

A Not-a-PLC **service** is one config entry: it has its own program, its own
entities and its own scan loop. You can run several side by side (for example one
per room or per subsystem).

**Create one:**

1. *Settings → Devices & Services → Add integration → **Not-a-PLC***.
2. Give it a name — this becomes the device name and the entity prefix, so a tag
   `daylight` in a service called `Garden` becomes `binary_sensor.garden_daylight`.
3. Pick a **starter program** and a **scan interval**.
4. Done — the service starts scanning immediately.

Repeat for each additional program.

## Editing a program

Everything is done in the UI — there is no YAML or JSON to hand-edit.

Open **Not-a-PLC** in the Home Assistant sidebar (admin only). The editor page has
three blocks:

**1. Define — tags and function blocks**

*Tags* are your variables. Each has a **kind**:

| Kind | Meaning |
|------|---------|
| `input` | Reads a Home Assistant entity (optionally one of its *attributes*, e.g. a light's `brightness`) |
| `output` | A result; published as an entity, and can optionally write back to a real entity |
| `memory` | Internal state that survives each scan; tick *retain* to survive a restart too |
| `temp` | Scratch value, reset every scan |

and a **type**: `BOOL` (on/off), `REAL` (a number) or `TIME`.

*Function blocks* are the stateful pieces — timers (`TON`, `TOF`, `TP`), counters
(`CTU`, `CTD`), latches (`SR`, `RS`) and edge detection (`R_TRIG`, `F_TRIG`). You
do **not** have to declare them up front: just drag one onto a rung and configure
it there.

**2. Toolbar — the palette**

Drag an element from the palette onto a rung, or click a tool and then click a
**＋** slot. Available elements:

- `] [` / `]/[` — normally-open / normally-closed contact
- `[ > ]` — comparator (`>`, `<`, `=`, …) on numeric values
- `OR` — a parallel branch; `NOT` — inverts the power at that point
- Function blocks — `TON`, `TOF`, `TP`, `CTU`, `CTD`, `SR`, `RS`, `R_TRIG`, `F_TRIG`
- `( )` — a coil (the output of the rung)
- `:=` — move a number into a REAL tag; `+ − × ÷` — calculate into a REAL tag
- `do` — call a Home Assistant service (activate a scene, set a preset, …) on the
  rung's rising edge

**3. Canvas — the ladder**

This is the live program: it is coloured in real time as it runs, so you can watch
power flow through your logic while you edit.

- **Click** an element to select it; **click again** to open its parameter popup.
- **Drag** an element to move it within the rung.
- Use **Small / Medium / Large** to zoom if a network is large.
- The validation bar flags problems and marks the offending position in red.

**Saving:** changes are not live until you press **Save**. The program is then
validated, stored, and the service reloads with it. If validation fails, the error
tells you which rung is at fault.

There is also an **Advanced → edit as text (DSL)** section: a lossless text form of
the same program, handy for copying a program between services or into git.

## Develop

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

ruff check . && ruff format --check .
mypy custom_components/not_a_plc
pytest -q
```

The engine (`custom_components/not_a_plc/engine/`) is pure standard library and
never imports `homeassistant`, so its tests run fast and without Home Assistant;
the integration tests use `pytest-homeassistant-custom-component`.

## License

MIT — see [LICENSE](LICENSE). You may use, modify and redistribute this freely,
including commercially; the only condition is that the copyright notice and
licence text travel with copies. It comes with no warranty.
