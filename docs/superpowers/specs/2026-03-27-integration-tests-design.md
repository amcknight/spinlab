# Integration Tests: Lua↔Python Poke Engine

**Date:** 2026-03-27
**Status:** Approved

## Problem

After changes to `spinlab.lua` or the Python side, manual smoke testing is required to catch regressions in the Lua↔Python IPC boundary. Bugs are caught late. There are no automated tests that exercise the real Lua script running inside Mesen2 communicating with Python over TCP.

## Solution

A **poke engine** test harness that runs `spinlab.lua` inside Mesen2's `--testrunner` headless mode. Python writes memory values on a frame schedule (via `.poke` scenario files), and `spinlab.lua`'s `detect_transitions()` fires naturally. Python connects over TCP, collects events, and asserts correctness.

Two test layers, same infrastructure:
1. **Memory-poke tests** (available now) — synthetic memory writes trigger transitions deterministically.
2. **`.spinrec` replay tests** (future, requires controller) — real recorded inputs replay through Mesen2's native replay mode.

## Architecture

### Component Diagram

```
pytest (conftest.py)
  │
  ├── launches: Mesen.exe --testrunner <rom> lua/poke_engine.lua
  │                          │
  │                          ├── poke_engine.lua
  │                          │     registers startFrame callback (poke injector)
  │                          │     dofile("spinlab.lua")
  │                          │       registers its own startFrame + inputPolled
  │                          │
  │                          │   Frame callback order:
  │                          │     1. poke_engine writes memory via emu.write()
  │                          │     2. spinlab reads memory via emu.read()
  │                          │     3. spinlab runs detect_transitions()
  │                          │     4. spinlab handles TCP (sends events)
  │                          │
  └── connects: TcpManager ←──TCP──→ spinlab.lua TCP server
        sends: game_context
        sends: poke_scenario (JSON)
        receives: transition events
        asserts: event sequence correctness
```

### Message Flow

```
Python                          Lua (poke_engine + spinlab)
  │                                │
  │  ← rom_info ──────────────────│  (spinlab sends on connect)
  │  game_context ────────────────→│  (spinlab handles normally)
  │  poke_scenario ───────────────→│  (poke_engine intercepts)
  │                                │  ... frames execute, pokes fire ...
  │  ← level_entrance ────────────│
  │  ← level_exit ────────────────│
  │                                │  settle_frames elapse
  │  ← disconnect (emu.stop) ─────│
```

## `.poke` Scenario Format

Each scenario is a `.poke` text file describing memory writes keyed by frame number.

### Syntax

```
# comment lines start with #
settle: 30              # header directive: frames to wait after last poke

1: game_mode=20 level_num=0x105
2: level_start=1
15: exit_mode=1 fanfare=1
```

- Each line is `frame: addr=value addr=value ...`
- Comments start with `#`
- Header directives (`settle:`, `description:`) appear before the first frame line
- Address names map to SNES memory addresses (see table below)
- Values are decimal by default; hex with `0x` prefix
- All values are explicit (`level_start=1`, not bare `level_start`)

### Address Name Table

| Name | SNES Address | Notes |
|------|-------------|-------|
| `game_mode` | `0x0100` | 20 = in level |
| `level_num` | `0x13BF` | Current level number |
| `room_num` | `0x010B` | Current room/sublevel |
| `level_start` | `0x1935` | 0→1 when player appears in level |
| `player_anim` | `0x0071` | 9 = death animation |
| `exit_mode` | `0x0DD5` | 0 = not exiting, non-zero = exiting |
| `io_port` | `0x1DFB` | 3=orb, 4=goal, 7=key, 8=fadeout |
| `fanfare` | `0x0906` | 1 = goal reached |
| `boss_defeat` | `0x13C6` | 0 = alive, non-zero = defeated |
| `midway` | `0x13CE` | 0→1 when checkpoint tape touched |
| `cp_entrance` | `0x1B403` | ASM-style checkpoint entrance |

## Poke Engine (`lua/poke_engine.lua`)

### Boot Sequence

1. Register `startFrame` callback (poke injector)
2. `dofile("spinlab.lua")` — registers its own `startFrame` + `inputPolled` callbacks
3. Mesen fires frame callbacks in registration order: poke_engine writes → spinlab reads

### TCP Message Interception

`spinlab.lua` gets a minimal addition — a global hook for unrecognized messages:

```lua
-- at end of handle_tcp's command parsing:
if poke_handler then poke_handler(line) end
```

`poke_engine.lua` sets the global `poke_handler` before `dofile`. This lets the engine intercept the `poke_scenario` message without modifying spinlab's core logic. The hook is also useful for future extension scripts.

### Frame Execution

The engine maintains its own frame counter (spinlab's `frame_counter` is local). On each frame:

1. If no scenario loaded yet, return early
2. Compute `rel_frame = own_counter - scenario_start_frame`
3. If `poke_schedule[rel_frame]` exists, call `emu.write(addr, value, emu.memType.snesMemory)` for each poke
4. If `rel_frame > last_poke_frame + settle_frames`, call `emu.stop(0)`

### JSON Parsing

The poke engine receives the scenario as a JSON message over TCP. Since spinlab.lua's JSON helpers (`json_get_str`, `json_get_num`) are `local` and not accessible after `dofile`, the poke engine includes its own minimal JSON parser for the `poke_scenario` message — just enough to extract the pokes array (frame, addr, value triples) and the settle_frames directive.

## Python Test Infrastructure

### File Layout

```
tests/
└── integration/
    ├── conftest.py            # Fixtures: mesen launcher, tcp client, event collector
    ├── addresses.py           # ADDR_* constants mirroring Lua
    ├── poke_parser.py         # .poke file → poke_scenario JSON
    ├── test_transitions.py    # Poke-based transition detection scenarios
    ├── test_reference_flow.py # Full reference capture via poke sequences
    ├── test_practice_flow.py  # Practice load → complete → rate via pokes
    └── scenarios/
        ├── entrance_goal.poke
        ├── entrance_death_spawn.poke
        ├── checkpoint_cold_spawn.poke
        ├── key_exit.poke
        ├── orb_exit.poke
        ├── same_frame_exit_entrance.poke
        └── reference_capture.poke
```

### Fixtures (`conftest.py`)

1. **`mesen_process`** — Launches `Mesen.exe --testrunner <rom> lua/poke_engine.lua`. Yields the subprocess, kills on teardown if `emu.stop()` didn't fire. ROM path from `config.yaml` (`emulator.rom_dir` + test ROM), with `MESEN_PATH` env var override. The `.poke` file is not passed to Mesen — Python parses it and sends the scenario over TCP.

2. **`tcp_client(mesen_process)`** — Creates `TcpManager`, connects with retry (Mesen2 needs a moment to boot), sends `game_context` with test game ID. Yields connected client. Disconnects on teardown.

3. **`run_scenario`** — High-level fixture combining the above. Parses the `.poke` file, sends `poke_scenario` over TCP, collects events until disconnect or timeout. Returns the ordered event list.

### Pytest Configuration

- Marker: `@pytest.mark.integration` — requires Mesen2 installed
- Default: skipped via `addopts = -m "not integration"` in pytest config
- Run explicitly: `pytest -m integration`
- Mesen2 path: `config.yaml` → `emulator.path`, overridden by `MESEN_PATH` env var

## Scenario Library

### Initial Scenarios

| File | Tests | Expected Events |
|------|-------|----------------|
| `entrance_goal.poke` | Enter level → normal goal | `level_entrance`, `level_exit(goal=normal)` |
| `entrance_death_spawn.poke` | Enter → die → respawn | `level_entrance`, `death`, `spawn` |
| `checkpoint_cold_spawn.poke` | Enter → midway CP → die → respawn | `level_entrance`, `checkpoint(cp_ordinal=1)`, `death`, `spawn(is_cold_cp=true)` |
| `key_exit.poke` | Enter → key exit | `level_entrance`, `level_exit(goal=key)` |
| `orb_exit.poke` | Enter → orb exit | `level_entrance`, `level_exit(goal=orb)` |
| `same_frame_exit_entrance.poke` | Exit + entrance on same frame | `level_exit` only, entrance suppressed |
| `reference_capture.poke` | Full reference flow | All transitions + `rec_saved`, state paths |

### Practice Flow Tests

Use the same poke engine but the Python side sends `practice_load` before pokes begin, then asserts on `attempt_result` events. Scenarios for: successful completion, death during practice, auto-advance timing.

## Future: `.spinrec` Replay Tests

When real controller recordings are available:

1. Record a short level using SpinLab's reference capture (creates `.spinrec` + `.mss`)
2. Place files in `tests/integration/scenarios/`
3. `conftest.py` provides a `run_replay` fixture that sends a `replay` command instead of `poke_scenario`
4. Same event collection and assertion patterns
5. Poke engine is not involved — spinlab.lua's native replay mode handles input injection

The infrastructure (fixtures, event collection, assertion helpers) is shared between poke tests and replay tests.

## How to Add a New Scenario

1. Create `tests/integration/scenarios/my_scenario.poke`:

   ```
   # my_scenario.poke — Short description of what this tests
   settle: 30

   1: game_mode=20 level_num=0x105
   2: level_start=1
   10: player_anim=9
   ```

2. Use address names from the Address Name Table above.

3. Add a test function in `tests/integration/test_transitions.py` (or a new test file):

   ```python
   @pytest.mark.integration
   async def test_my_scenario(run_scenario):
       events = await run_scenario("my_scenario.poke")
       deaths = [e for e in events if e["event"] == "death"]
       assert len(deaths) == 1
   ```

4. Run: `pytest -m integration`

## Risks and Mitigations

| Risk | Mitigation |
|------|-----------|
| `emu.write()` doesn't affect same-frame `emu.read()` | Verify in PoC; if needed, poke one frame before the intended read frame |
| `dofile("spinlab.lua")` fails in testrunner context | `emu.getScriptDataFolder()` and TCP init may behave differently; test early |
| Mesen2 startup too slow for fast test iteration | Subprocess is per-test; consider session-scoped fixture for batch runs |
| `.poke` parser edge cases | Keep format minimal; validate in parser with clear error messages |
| `frame_counter` sync between engine and spinlab | Engine uses independent counter; both increment on same callback event |

## Dependencies

- Mesen2 installed and accessible (not a pip dependency)
- A ROM file for headless loading (Love Yourself.emc from config)
- Existing: `pytest`, `pytest-asyncio`, `spinlab.tcp_manager`
