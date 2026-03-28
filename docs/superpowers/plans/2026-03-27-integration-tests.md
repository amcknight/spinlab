# Integration Tests: Poke Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a headless Mesen2 integration test harness that exercises the real Lua↔Python IPC boundary using memory-poke scenarios.

**Architecture:** A Lua poke engine (`lua/poke_engine.lua`) wraps `spinlab.lua` via `dofile`, receives frame-keyed memory writes over TCP from Python, and injects them via `emu.write()` before spinlab's `read_mem()` runs. Python pytest fixtures launch Mesen2 headless (`--testrunner`), connect via `TcpManager`, send scenarios parsed from `.poke` files, collect events, and assert correctness.

**Tech Stack:** Lua (Mesen2 API), Python 3.11+, pytest, pytest-asyncio, existing `TcpManager`

**Spec:** `docs/superpowers/specs/2026-03-27-integration-tests-design.md`

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `lua/poke_engine.lua` | Create | Test harness: registers poke callback, dofiles spinlab.lua, executes poke schedule, calls emu.stop() |
| `lua/spinlab.lua` | Modify (line ~982) | Add 1-line `poke_handler` hook before "unknown command" error |
| `tests/integration/__init__.py` | Create | Package marker |
| `tests/integration/conftest.py` | Create | Fixtures: mesen_process, tcp_client, run_scenario, collect_events |
| `tests/integration/addresses.py` | Create | ADDR_* constants mirroring Lua, used by .poke parser |
| `tests/integration/poke_parser.py` | Create | Parse .poke files into poke_scenario JSON messages |
| `tests/integration/test_poke_parser.py` | Create | Unit tests for .poke parser (no Mesen2 needed) |
| `tests/integration/test_transitions.py` | Create | Integration tests: transition detection scenarios |
| `tests/integration/scenarios/entrance_goal.poke` | Create | Scenario: level entrance → normal goal exit |
| `tests/integration/scenarios/entrance_death_spawn.poke` | Create | Scenario: entrance → death → respawn |
| `tests/integration/scenarios/checkpoint_cold_spawn.poke` | Create | Scenario: entrance → midway CP → death → cold spawn |
| `tests/integration/scenarios/key_exit.poke` | Create | Scenario: entrance → key exit |
| `tests/integration/scenarios/same_frame_exit_entrance.poke` | Create | Scenario: exit+entrance on same frame |
| `pyproject.toml` | Modify | Add integration marker config |

---

### Task 1: Add poke_handler hook to spinlab.lua

**Files:**
- Modify: `lua/spinlab.lua:981-983`

This is the smallest possible change — a 1-line global hook that the poke engine will set.

- [ ] **Step 1: Add the hook**

In `lua/spinlab.lua`, find the `tcp_dispatch` function. Right before the `client:send("err:unknown_command\n")` fallback on line 983, add the hook:

```lua
  -- Extension hook: let external scripts handle unrecognized messages
  if poke_handler then
    poke_handler(line)
    return
  end

  client:send("err:unknown_command\n")
```

The existing code at lines 981-983 currently reads:
```lua
    end
  end

  client:send("err:unknown_command\n")
end
```

Change it to:
```lua
    end
  end

  -- Extension hook: let external scripts handle unrecognized messages
  if poke_handler then
    poke_handler(line)
    return
  end

  client:send("err:unknown_command\n")
end
```

- [ ] **Step 2: Verify no regressions manually**

Open Mesen2 with a ROM and spinlab.lua, connect the dashboard, confirm normal operation is unchanged. The `poke_handler` global is nil by default so the hook is a no-op.

- [ ] **Step 3: Commit**

```bash
git add lua/spinlab.lua
git commit -m "feat: add poke_handler extension hook to tcp_dispatch"
```

---

### Task 2: Create the .poke parser (Python)

**Files:**
- Create: `tests/integration/__init__.py`
- Create: `tests/integration/addresses.py`
- Create: `tests/integration/poke_parser.py`
- Create: `tests/integration/test_poke_parser.py`

- [ ] **Step 1: Create package and address constants**

Create `tests/integration/__init__.py` (empty file).

Create `tests/integration/addresses.py`:

```python
"""SNES memory address constants mirroring lua/spinlab.lua lines 43-53."""

ADDR_MAP: dict[str, int] = {
    "game_mode":    0x0100,
    "level_num":    0x13BF,
    "room_num":     0x010B,
    "level_start":  0x1935,
    "player_anim":  0x0071,
    "exit_mode":    0x0DD5,
    "io_port":      0x1DFB,
    "fanfare":      0x0906,
    "boss_defeat":  0x13C6,
    "midway":       0x13CE,
    "cp_entrance":  0x1B403,
}
```

- [ ] **Step 2: Write failing tests for the .poke parser**

Create `tests/integration/test_poke_parser.py`:

```python
"""Tests for .poke scenario file parser."""

import json
import pytest
from tests.integration.poke_parser import parse_poke


SIMPLE_SCENARIO = """\
# entrance_goal.poke — Level entrance then normal goal
settle: 30

1: game_mode=20 level_num=0x105
2: level_start=1
15: exit_mode=1 fanfare=1
"""


def test_parse_header_settle():
    result = parse_poke(SIMPLE_SCENARIO)
    assert result["settle_frames"] == 30


def test_parse_poke_count():
    result = parse_poke(SIMPLE_SCENARIO)
    assert len(result["pokes"]) == 6  # 2 + 1 + 2 + 1 (individual addr=value pairs, but grouped by frame... no, flattened)


def test_parse_frame_1_pokes():
    result = parse_poke(SIMPLE_SCENARIO)
    frame_1 = [p for p in result["pokes"] if p["frame"] == 1]
    assert len(frame_1) == 2
    addrs = {p["addr"] for p in frame_1}
    assert 0x0100 in addrs  # game_mode
    assert 0x13BF in addrs  # level_num


def test_parse_hex_value():
    result = parse_poke(SIMPLE_SCENARIO)
    level_poke = [p for p in result["pokes"] if p["addr"] == 0x13BF][0]
    assert level_poke["value"] == 0x105  # 261 decimal


def test_parse_decimal_value():
    result = parse_poke(SIMPLE_SCENARIO)
    gm_poke = [p for p in result["pokes"] if p["addr"] == 0x0100][0]
    assert gm_poke["value"] == 20


def test_parse_frame_15():
    result = parse_poke(SIMPLE_SCENARIO)
    frame_15 = [p for p in result["pokes"] if p["frame"] == 15]
    assert len(frame_15) == 2


def test_comments_and_blank_lines_ignored():
    scenario = "# just a comment\n\nsettle: 10\n\n# another comment\n1: game_mode=20\n"
    result = parse_poke(scenario)
    assert len(result["pokes"]) == 1


def test_unknown_address_raises():
    scenario = "settle: 10\n1: bogus_addr=42\n"
    with pytest.raises(ValueError, match="Unknown address name"):
        parse_poke(scenario)


def test_default_settle():
    scenario = "1: game_mode=20\n"
    result = parse_poke(scenario)
    assert result["settle_frames"] == 30  # default


def test_output_is_json_serializable():
    result = parse_poke(SIMPLE_SCENARIO)
    # Should not raise
    json.dumps(result)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/integration/test_poke_parser.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tests.integration.poke_parser'`

- [ ] **Step 4: Implement the parser**

Create `tests/integration/poke_parser.py`:

```python
"""Parse .poke scenario files into poke_scenario JSON-serializable dicts.

Format:
    # comment
    settle: 30

    1: game_mode=20 level_num=0x105
    2: level_start=1
    15: exit_mode=1 fanfare=1

Each line is  frame: name=value name=value ...
Address names are resolved via addresses.ADDR_MAP.
Values are decimal by default; hex with 0x prefix.
"""
from __future__ import annotations

from tests.integration.addresses import ADDR_MAP

DEFAULT_SETTLE = 30


def _parse_value(s: str) -> int:
    """Parse a decimal or hex integer string."""
    s = s.strip()
    if s.startswith("0x") or s.startswith("0X"):
        return int(s, 16)
    return int(s)


def parse_poke(text: str) -> dict:
    """Parse .poke file content into a poke_scenario dict.

    Returns:
        {"event": "poke_scenario", "settle_frames": int,
         "pokes": [{"frame": int, "addr": int, "value": int}, ...]}
    """
    settle_frames = DEFAULT_SETTLE
    pokes: list[dict] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()

        # Skip comments and blank lines
        if not line or line.startswith("#"):
            continue

        # Header directive: settle
        if line.startswith("settle:"):
            settle_frames = int(line.split(":", 1)[1].strip())
            continue

        # Frame line: "N: addr=val addr=val ..."
        if ":" not in line:
            continue

        frame_str, rest = line.split(":", 1)
        frame = int(frame_str.strip())

        for token in rest.strip().split():
            if "=" not in token:
                raise ValueError(f"Invalid poke token (missing =): {token!r}")
            name, val_str = token.split("=", 1)
            name = name.strip()
            if name not in ADDR_MAP:
                raise ValueError(f"Unknown address name: {name!r}")
            pokes.append({
                "frame": frame,
                "addr": ADDR_MAP[name],
                "value": _parse_value(val_str),
            })

    return {
        "event": "poke_scenario",
        "settle_frames": settle_frames,
        "pokes": pokes,
    }


def parse_poke_file(path: str) -> dict:
    """Read and parse a .poke file from disk."""
    with open(path) as f:
        return parse_poke(f.read())
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/integration/test_poke_parser.py -v`
Expected: All 10 tests PASS

- [ ] **Step 6: Commit**

```bash
git add tests/integration/__init__.py tests/integration/addresses.py tests/integration/poke_parser.py tests/integration/test_poke_parser.py
git commit -m "feat: .poke scenario parser with address map and tests"
```

---

### Task 3: Create the Lua poke engine

**Files:**
- Create: `lua/poke_engine.lua`

- [ ] **Step 1: Write the poke engine**

Create `lua/poke_engine.lua`:

```lua
-- Poke Engine — Integration test harness for spinlab.lua
--
-- Usage: Mesen.exe --testrunner <rom> lua/poke_engine.lua
--
-- Boot sequence:
--   1. This script registers a startFrame callback (poke injector)
--   2. dofile loads spinlab.lua, which registers its own callbacks
--   3. Mesen fires callbacks in registration order:
--      poke_engine emu.write() → spinlab emu.read() → detect_transitions()
--
-- Protocol:
--   After Python connects and sends game_context, it sends a poke_scenario
--   JSON message. The engine parses the poke schedule, then on each frame
--   writes the scheduled values to SNES memory. After the last poke plus
--   settle_frames, it calls emu.stop(0).

local SNES = emu.memType.snesMemory

-----------------------------------------------------------------------
-- ADDRESS MAP (must match spinlab.lua lines 43-53)
-----------------------------------------------------------------------
local ADDR_MAP = {
  game_mode    = 0x0100,
  level_num    = 0x13BF,
  room_num     = 0x010B,
  level_start  = 0x1935,
  player_anim  = 0x0071,
  exit_mode    = 0x0DD5,
  io_port      = 0x1DFB,
  fanfare      = 0x0906,
  boss_defeat  = 0x13C6,
  midway       = 0x13CE,
  cp_entrance  = 0x1B403,
}

-----------------------------------------------------------------------
-- STATE
-----------------------------------------------------------------------
local poke_schedule = {}   -- {[frame_number] = {{addr=int, value=int}, ...}}
local scenario_loaded = false
local scenario_start_frame = nil
local last_poke_frame = 0
local settle_frames = 30
local own_frame_counter = 0

-----------------------------------------------------------------------
-- MINIMAL JSON PARSER (for poke_scenario message)
-----------------------------------------------------------------------
-- We only need to parse: {"event":"poke_scenario","settle_frames":N,"pokes":[...]}
-- where each poke is {"frame":N,"addr":N,"value":N}

local function parse_poke_scenario(json_str)
  -- Extract settle_frames
  local sf = json_str:match('"settle_frames"%s*:%s*(%d+)')
  if sf then settle_frames = tonumber(sf) end

  -- Extract pokes array and iterate over objects
  local pokes_str = json_str:match('"pokes"%s*:%s*(%[.-%])')
  if not pokes_str then
    emu.log("[PokeEngine] ERROR: no pokes array found")
    return false
  end

  -- Match each {...} object in the array
  for obj in pokes_str:gmatch("{(.-)}") do
    local frame = tonumber(obj:match('"frame"%s*:%s*(%d+)'))
    local addr  = tonumber(obj:match('"addr"%s*:%s*(%d+)'))
    local value = tonumber(obj:match('"value"%s*:%s*(%d+)'))
    if frame and addr and value then
      if not poke_schedule[frame] then
        poke_schedule[frame] = {}
      end
      table.insert(poke_schedule[frame], {addr = addr, value = value})
      if frame > last_poke_frame then
        last_poke_frame = frame
      end
    end
  end

  emu.log("[PokeEngine] Loaded scenario: " .. last_poke_frame .. " frames + " .. settle_frames .. " settle")
  return true
end

-----------------------------------------------------------------------
-- POKE HANDLER (set as global before dofile so spinlab.lua can call it)
-----------------------------------------------------------------------
poke_handler = function(line)
  if line:sub(1, 1) ~= "{" then return end
  local event = line:match('"event"%s*:%s*"(.-)"')
  if event == "poke_scenario" then
    if parse_poke_scenario(line) then
      scenario_loaded = true
      -- Will set scenario_start_frame on next on_poke_frame call
    end
  end
end

-----------------------------------------------------------------------
-- FRAME CALLBACK (registered BEFORE spinlab.lua's dofile)
-----------------------------------------------------------------------
local function on_poke_frame()
  own_frame_counter = own_frame_counter + 1

  if not scenario_loaded then return end

  -- Set start frame on first frame after scenario load
  if not scenario_start_frame then
    scenario_start_frame = own_frame_counter
    emu.log("[PokeEngine] Scenario starts at frame " .. scenario_start_frame)
  end

  local rel_frame = own_frame_counter - scenario_start_frame

  -- Execute any pokes scheduled for this frame
  local pokes = poke_schedule[rel_frame]
  if pokes then
    for _, p in ipairs(pokes) do
      emu.write(p.addr, p.value, SNES)
    end
  end

  -- Stop after settle window
  if rel_frame > last_poke_frame + settle_frames then
    emu.log("[PokeEngine] Scenario complete, stopping emulator")
    emu.stop(0)
  end
end

-- Register BEFORE dofile so this fires before spinlab's on_start_frame
emu.addEventCallback(on_poke_frame, emu.eventType.startFrame)

-----------------------------------------------------------------------
-- LOAD SPINLAB
-----------------------------------------------------------------------
-- dofile executes spinlab.lua which registers its own callbacks.
-- Since on_poke_frame was registered first, emu.write() happens before
-- spinlab's read_mem() on each frame.
local script_dir = debug.getinfo(1, "S").source:match("@?(.*[\\/])")
dofile(script_dir .. "spinlab.lua")

emu.log("[PokeEngine] Harness loaded, waiting for poke_scenario command")
```

- [ ] **Step 2: Commit**

```bash
git add lua/poke_engine.lua
git commit -m "feat: poke engine test harness for headless Mesen2 integration tests"
```

---

### Task 4: Create pytest fixtures (conftest.py)

**Files:**
- Create: `tests/integration/conftest.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Add integration marker to pyproject.toml**

In `pyproject.toml`, add the marker registration and default exclusion to the existing `[tool.pytest.ini_options]` section:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
markers = ["integration: requires Mesen2 headless (--testrunner)"]
```

Note: we do NOT add `-m "not integration"` to addopts. Instead, integration tests will skip themselves at runtime if Mesen2 is not found. This is friendlier — running `pytest` shows the skips rather than silently hiding them.

- [ ] **Step 2: Write conftest.py**

Create `tests/integration/conftest.py`:

```python
"""Pytest fixtures for Mesen2 headless integration tests.

Fixtures:
    mesen_process  — launches Mesen.exe --testrunner, yields subprocess
    tcp_client     — connects TcpManager, sends game_context, yields client
    run_scenario   — parses .poke file, sends scenario, collects events
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import AsyncGenerator

import pytest
import yaml

from spinlab.tcp_manager import TcpManager
from tests.integration.poke_parser import parse_poke_file

# Resolve project paths
PROJECT_ROOT = Path(__file__).resolve().parents[2]
LUA_DIR = PROJECT_ROOT / "lua"
SCENARIO_DIR = Path(__file__).resolve().parent / "scenarios"

# Test game context
TEST_GAME_ID = "integration_test_"
TEST_GAME_NAME = "Integration Test ROM"


def _load_config() -> dict:
    """Load config.yaml from project root."""
    config_path = PROJECT_ROOT / "config.yaml"
    if config_path.exists():
        with open(config_path) as f:
            return yaml.safe_load(f)
    return {}


def _mesen_path() -> str | None:
    """Resolve Mesen2 executable path from env var or config."""
    env_path = os.environ.get("MESEN_PATH")
    if env_path:
        return env_path
    config = _load_config()
    return config.get("emulator", {}).get("path")


def _test_rom_path() -> str | None:
    """Resolve a ROM path for testing."""
    env_rom = os.environ.get("SPINLAB_TEST_ROM")
    if env_rom:
        return env_rom
    config = _load_config()
    rom_dir = config.get("rom", {}).get("dir")
    if rom_dir:
        # Use first .sfc/.smc/.emc file found
        rom_path = Path(rom_dir)
        for ext in ("*.sfc", "*.smc", "*.emc"):
            roms = list(rom_path.glob(ext))
            if roms:
                return str(roms[0])
    return None


def _tcp_port() -> int:
    """Resolve TCP port from config or default."""
    config = _load_config()
    return config.get("network", {}).get("port", 15482)


# Skip all integration tests if Mesen2 not available
_mesen = _mesen_path()
_rom = _test_rom_path()

pytestmark = pytest.mark.integration

skip_no_mesen = pytest.mark.skipif(
    not _mesen or not Path(_mesen).exists(),
    reason=f"Mesen2 not found (MESEN_PATH or config.yaml emulator.path): {_mesen}",
)
skip_no_rom = pytest.mark.skipif(
    not _rom or not Path(_rom).exists(),
    reason=f"Test ROM not found (SPINLAB_TEST_ROM or config.yaml rom.dir): {_rom}",
)


@pytest.fixture
async def mesen_process():
    """Launch Mesen2 in --testrunner mode with poke_engine.lua."""
    if not _mesen or not _rom:
        pytest.skip("Mesen2 or test ROM not configured")

    poke_engine = str(LUA_DIR / "poke_engine.lua")
    cmd = [_mesen, "--testrunner", _rom, poke_engine]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Give Mesen2 a moment to start up and open TCP
    await asyncio.sleep(1.0)

    yield proc

    # Teardown: kill if still running
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


@pytest.fixture
async def tcp_client(mesen_process) -> AsyncGenerator[TcpManager, None]:
    """Connect TcpManager to the Lua TCP server with retry."""
    port = _tcp_port()
    client = TcpManager("127.0.0.1", port)

    # Retry connection — Mesen2 may need time to start TCP server
    connected = False
    for attempt in range(10):
        connected = await client.connect(timeout=2.0)
        if connected:
            break
        await asyncio.sleep(0.5)

    if not connected:
        pytest.fail("Could not connect to Lua TCP server after 10 attempts")

    # Wait for rom_info event
    rom_event = await client.recv_event(timeout=5.0)
    assert rom_event is not None, "Did not receive rom_info from Lua"
    assert rom_event.get("event") == "rom_info"

    # Send game_context
    await client.send(json.dumps({
        "event": "game_context",
        "game_id": TEST_GAME_ID,
        "game_name": TEST_GAME_NAME,
    }))

    yield client

    await client.disconnect()


@pytest.fixture
def run_scenario(tcp_client):
    """High-level fixture: parse .poke file, send scenario, collect events."""

    async def _run(scenario_name: str, timeout: float = 15.0) -> list[dict]:
        """Send a poke scenario and collect all events until disconnect.

        Args:
            scenario_name: filename in tests/integration/scenarios/
            timeout: max seconds to wait for scenario completion

        Returns:
            Ordered list of event dicts received from Lua.
        """
        scenario_path = SCENARIO_DIR / scenario_name
        if not scenario_path.exists():
            pytest.fail(f"Scenario file not found: {scenario_path}")

        scenario = parse_poke_file(str(scenario_path))
        await tcp_client.send(json.dumps(scenario))

        # Collect events until connection drops (emu.stop) or timeout
        events: list[dict] = []
        try:
            while True:
                event = await tcp_client.recv_event(timeout=timeout)
                if event is None:
                    break  # timeout
                events.append(event)
        except (ConnectionError, OSError):
            pass  # connection closed by emu.stop — expected

        return events

    return _run
```

- [ ] **Step 3: Commit**

```bash
git add tests/integration/conftest.py pyproject.toml
git commit -m "feat: pytest fixtures for Mesen2 headless integration tests"
```

---

### Task 5: Create .poke scenario files

**Files:**
- Create: `tests/integration/scenarios/entrance_goal.poke`
- Create: `tests/integration/scenarios/entrance_death_spawn.poke`
- Create: `tests/integration/scenarios/checkpoint_cold_spawn.poke`
- Create: `tests/integration/scenarios/key_exit.poke`
- Create: `tests/integration/scenarios/same_frame_exit_entrance.poke`

- [ ] **Step 1: Create entrance_goal.poke**

Create `tests/integration/scenarios/entrance_goal.poke`:

```
# entrance_goal.poke — Level entrance followed by normal goal exit
# Expects: level_entrance, level_exit(goal=normal)
settle: 30

1: game_mode=20 level_num=0x105 room_num=1
2: level_start=1
15: exit_mode=1 fanfare=1
```

- [ ] **Step 2: Create entrance_death_spawn.poke**

Create `tests/integration/scenarios/entrance_death_spawn.poke`:

```
# entrance_death_spawn.poke — Enter level, die, respawn
# Expects: level_entrance, death, spawn
settle: 30

1: game_mode=20 level_num=0x105 room_num=1
2: level_start=1
15: player_anim=9
25: player_anim=0 level_start=0
26: level_start=1
```

- [ ] **Step 3: Create checkpoint_cold_spawn.poke**

Create `tests/integration/scenarios/checkpoint_cold_spawn.poke`:

```
# checkpoint_cold_spawn.poke — Enter, hit midway checkpoint, die, cold respawn
# Expects: level_entrance, checkpoint(cp_ordinal=1), death, spawn(is_cold_cp=true)
settle: 30

1: game_mode=20 level_num=0x105 room_num=1
2: level_start=1
10: midway=1
20: player_anim=9
30: player_anim=0 level_start=0 midway=0
31: level_start=1
```

- [ ] **Step 4: Create key_exit.poke**

Create `tests/integration/scenarios/key_exit.poke`:

```
# key_exit.poke — Enter level, exit with key
# Expects: level_entrance, level_exit(goal=key)
settle: 30

1: game_mode=20 level_num=0x105 room_num=1
2: level_start=1
15: exit_mode=1 io_port=7
```

- [ ] **Step 5: Create orb_exit.poke**

Create `tests/integration/scenarios/orb_exit.poke`:

```
# orb_exit.poke — Enter level, exit with orb
# Expects: level_entrance, level_exit(goal=orb)
settle: 30

1: game_mode=20 level_num=0x105 room_num=1
2: level_start=1
15: exit_mode=1 io_port=3
```

- [ ] **Step 6: Create same_frame_exit_entrance.poke**

Create `tests/integration/scenarios/same_frame_exit_entrance.poke`:

```
# same_frame_exit_entrance.poke — Exit and entrance trigger on same frame
# Tests the ordering guard: exit_mode and level_start both 0→1 simultaneously.
# Expects: level_exit fires, entrance is suppressed
settle: 30

1: game_mode=20 level_num=0x105 room_num=1
2: level_start=1
15: exit_mode=1 fanfare=1 level_start=0
16: exit_mode=0 level_start=0
20: exit_mode=1 level_start=1
```

- [ ] **Step 7: Commit**

```bash
git add tests/integration/scenarios/
git commit -m "feat: .poke scenario files for integration tests"
```

---

### Task 6: Write integration tests

**Files:**
- Create: `tests/integration/test_transitions.py`

- [ ] **Step 1: Write the test file**

Create `tests/integration/test_transitions.py`:

```python
"""Integration tests: transition detection via memory pokes in headless Mesen2.

These tests require Mesen2 installed and a test ROM available.
Run with: pytest -m integration
Skip automatically if Mesen2 or ROM not found.
"""
from __future__ import annotations

import pytest

pytestmark = [pytest.mark.integration]


class TestEntranceGoal:
    """Level entrance followed by normal goal exit."""

    async def test_level_entrance_event(self, run_scenario):
        events = await run_scenario("entrance_goal.poke")
        entrances = [e for e in events if e["event"] == "level_entrance"]
        assert len(entrances) == 1
        assert entrances[0]["level"] == 0x105

    async def test_level_exit_event(self, run_scenario):
        events = await run_scenario("entrance_goal.poke")
        exits = [e for e in events if e["event"] == "level_exit"]
        assert len(exits) == 1
        assert exits[0]["goal"] == "normal"
        assert exits[0]["level"] == 0x105

    async def test_elapsed_time_positive(self, run_scenario):
        events = await run_scenario("entrance_goal.poke")
        exits = [e for e in events if e["event"] == "level_exit"]
        assert len(exits) == 1
        assert exits[0]["elapsed_ms"] > 0


class TestEntranceDeathSpawn:
    """Enter level, die, respawn."""

    async def test_death_event(self, run_scenario):
        events = await run_scenario("entrance_death_spawn.poke")
        deaths = [e for e in events if e["event"] == "death"]
        assert len(deaths) == 1

    async def test_spawn_event(self, run_scenario):
        events = await run_scenario("entrance_death_spawn.poke")
        spawns = [e for e in events if e["event"] == "spawn"]
        assert len(spawns) == 1

    async def test_event_order(self, run_scenario):
        events = await run_scenario("entrance_death_spawn.poke")
        event_names = [e["event"] for e in events]
        entrance_idx = event_names.index("level_entrance")
        death_idx = event_names.index("death")
        spawn_idx = event_names.index("spawn")
        assert entrance_idx < death_idx < spawn_idx


class TestCheckpointColdSpawn:
    """Enter, hit midway, die, cold respawn."""

    async def test_checkpoint_event(self, run_scenario):
        events = await run_scenario("checkpoint_cold_spawn.poke")
        cps = [e for e in events if e["event"] == "checkpoint"]
        assert len(cps) == 1
        assert cps[0]["cp_ordinal"] == 1

    async def test_cold_spawn(self, run_scenario):
        events = await run_scenario("checkpoint_cold_spawn.poke")
        spawns = [e for e in events if e["event"] == "spawn"]
        assert len(spawns) == 1
        assert spawns[0]["is_cold_cp"] is True

    async def test_full_event_sequence(self, run_scenario):
        events = await run_scenario("checkpoint_cold_spawn.poke")
        event_names = [e["event"] for e in events]
        assert "level_entrance" in event_names
        assert "checkpoint" in event_names
        assert "death" in event_names
        assert "spawn" in event_names


class TestKeyExit:
    """Enter level, exit with key."""

    async def test_key_goal_type(self, run_scenario):
        events = await run_scenario("key_exit.poke")
        exits = [e for e in events if e["event"] == "level_exit"]
        assert len(exits) == 1
        assert exits[0]["goal"] == "key"


class TestOrbExit:
    """Enter level, exit with orb."""

    async def test_orb_goal_type(self, run_scenario):
        events = await run_scenario("orb_exit.poke")
        exits = [e for e in events if e["event"] == "level_exit"]
        assert len(exits) == 1
        assert exits[0]["goal"] == "orb"


class TestSameFrameExitEntrance:
    """Exit and entrance on same frame — entrance should be suppressed."""

    async def test_entrance_suppressed(self, run_scenario):
        events = await run_scenario("same_frame_exit_entrance.poke")
        # There should be exactly one entrance (from frame 2) and two exits.
        # The frame-20 entrance (same frame as exit) should be suppressed.
        entrances = [e for e in events if e["event"] == "level_entrance"]
        exits = [e for e in events if e["event"] == "level_exit"]
        assert len(entrances) == 1, f"Expected 1 entrance, got {len(entrances)}: {entrances}"
        assert len(exits) == 2, f"Expected 2 exits, got {len(exits)}: {exits}"
```

- [ ] **Step 2: Commit**

```bash
git add tests/integration/test_transitions.py
git commit -m "feat: integration tests for transition detection via poke scenarios"
```

---

### Task 7: First smoke test — run against real Mesen2

**Files:** None (validation only)

This is the critical PoC step. We need to verify that the full pipeline works: Mesen2 headless → poke engine → spinlab.lua → TCP → Python.

- [ ] **Step 1: Run the poke parser unit tests**

Run: `pytest tests/integration/test_poke_parser.py -v`
Expected: All PASS (no Mesen2 needed)

- [ ] **Step 2: Run one integration test**

Run: `pytest tests/integration/test_transitions.py::TestEntranceGoal::test_level_entrance_event -v -m integration -s`
Expected: Either PASS or a clear error message indicating what needs fixing.

The `-s` flag shows stdout/stderr so we can see Mesen2's output and the poke engine's log messages.

- [ ] **Step 3: Debug and fix issues**

Likely issues to watch for:
- `emu.write()` timing: if writes don't take effect until next frame, shift all poke frames back by 1 in the scenario files
- `dofile` path resolution: the `script_dir` detection in poke_engine.lua may need adjustment for `--testrunner` mode
- TCP connection timing: may need to increase the retry count or sleep duration in conftest.py
- `emu.stop()` may not cleanly close the TCP socket: may need to handle the disconnect differently in the event collector

Fix any issues found in the relevant files.

- [ ] **Step 4: Run full integration suite**

Run: `pytest tests/integration/ -v -m integration -s`
Expected: All tests PASS (or skip if Mesen2/ROM not found)

- [ ] **Step 5: Commit any fixes**

```bash
git add -A
git commit -m "fix: integration test adjustments from first smoke test"
```

---

## Appendix: Future Tasks (not in this plan)

These are deferred until a controller is available or until the poke tests are stable:

- **Practice flow tests** (`test_practice_flow.py`): Python sends `practice_load` before pokes, asserts on `attempt_result`
- **Reference capture tests** (`test_reference_flow.py`): Python sends `reference_start`/`reference_stop`, asserts on `rec_saved` and save state files
- **`.spinrec` replay tests**: record real gameplay fixtures, replay through native replay mode
- **CI integration**: run integration tests in GitHub Actions with Mesen2 installed
