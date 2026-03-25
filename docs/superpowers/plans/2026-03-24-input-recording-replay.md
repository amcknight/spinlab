# Input Recording & Replay Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add always-on input recording to passive mode and a new replay mode so reference runs can be regenerated without human input, at any emulation speed.

**Architecture:** Recording piggybacks on passive mode via an `inputPolled` callback that captures controller state every frame into a `.spinrec` binary file. Replay is a new peer mode that feeds those inputs back via `emu.setInput()`. Both sides reuse the existing `detect_transitions()` pipeline so all segment events fire naturally. Python orchestrates via TCP commands (`reference_start`, `reference_stop`, `replay`, `replay_stop`).

**Tech Stack:** Lua (Mesen2 API), Python 3.11+ (asyncio, FastAPI), SQLite, pytest

**Spec:** `docs/superpowers/specs/2026-03-24-tas-replay-design.md`

---

## File Map

| Action | File | Responsibility |
|--------|------|----------------|
| Modify | `lua/spinlab.lua` | Add `inputPolled` callback, recording state, replay state, `send_event` wrapper, new TCP command handlers, `on_cpu_exec` extension for `pending_rec_save` |
| Modify | `python/spinlab/session_manager.py` | Add `start_replay()`/`stop_replay()`, extend `start_reference()`/`stop_reference()` to send TCP commands, handle `rec_saved`/`replay_*` events, source tagging |
| Modify | `python/spinlab/dashboard.py` | Add `/api/replay/start`, `/api/replay/stop` endpoints, SSE for replay progress |
| Modify | `python/spinlab/cli.py` | Add `spinlab replay` CLI command |
| Create | `python/spinlab/spinrec.py` | `.spinrec` file format reader (Python-side, for validation/inspection) |
| Create | `tests/test_spinrec.py` | Tests for `.spinrec` format read/write |
| Create | `tests/test_replay.py` | Tests for SessionManager replay flow |

---

## Task 1: `.spinrec` File Format (Python Reader)

Build the Python-side `.spinrec` reader first. This is a pure data module with no dependencies on Mesen2 — easy to TDD. Lua will write these files; Python needs to read them for validation and inspection.

**Files:**
- Create: `python/spinlab/spinrec.py`
- Create: `tests/test_spinrec.py`

- [ ] **Step 1: Write failing tests for header parsing**

```python
# tests/test_spinrec.py
"""Tests for .spinrec binary format."""
import struct
import pytest
from spinlab.spinrec import read_spinrec, write_spinrec, SpinrecHeader, MAGIC, VERSION


def make_spinrec(game_id: str = "abcdef0123456789", frames: list[int] | None = None) -> bytes:
    """Build a valid .spinrec binary blob."""
    frames = frames or [0, 0, 0]
    header = struct.pack("<4sH16sI6s", MAGIC, VERSION, game_id.encode("ascii"), len(frames), b"\x00" * 6)
    body = b"".join(struct.pack("<H", f) for f in frames)
    return header + body


class TestSpinrecRead:
    def test_reads_valid_file(self):
        data = make_spinrec(frames=[0x0000, 0x0011, 0x0FFF])
        header, frames = read_spinrec(data)
        assert header.magic == MAGIC
        assert header.version == VERSION
        assert header.game_id == "abcdef0123456789"
        assert header.frame_count == 3
        assert frames == [0x0000, 0x0011, 0x0FFF]

    def test_rejects_bad_magic(self):
        data = b"BAAD" + b"\x00" * 28
        with pytest.raises(ValueError, match="magic"):
            read_spinrec(data)

    def test_rejects_truncated_body(self):
        data = make_spinrec(frames=[1, 2, 3])
        truncated = data[:-2]  # chop last frame
        with pytest.raises(ValueError, match="truncated"):
            read_spinrec(truncated)

    def test_rejects_too_short_header(self):
        with pytest.raises(ValueError, match="header"):
            read_spinrec(b"\x00" * 10)


class TestSpinrecWrite:
    def test_roundtrip(self):
        frames = [0x0001, 0x0010, 0x0100]
        data = write_spinrec("abcdef0123456789", frames)
        header, read_frames = read_spinrec(data)
        assert header.game_id == "abcdef0123456789"
        assert read_frames == frames

    def test_empty_frames(self):
        data = write_spinrec("abcdef0123456789", [])
        header, frames = read_spinrec(data)
        assert header.frame_count == 0
        assert frames == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /c/Users/thedo/git/spinlab && python -m pytest tests/test_spinrec.py -v`
Expected: ImportError — `spinrec` module doesn't exist yet.

- [ ] **Step 3: Implement `spinrec.py`**

```python
# python/spinlab/spinrec.py
"""Read/write .spinrec binary input recording format."""
from __future__ import annotations

import struct
from dataclasses import dataclass

MAGIC = b"SREC"
VERSION = 1
HEADER_SIZE = 32
HEADER_FMT = "<4sH16sI6s"


@dataclass
class SpinrecHeader:
    magic: bytes
    version: int
    game_id: str
    frame_count: int


def read_spinrec(data: bytes) -> tuple[SpinrecHeader, list[int]]:
    """Parse a .spinrec binary blob into header + frame list."""
    if len(data) < HEADER_SIZE:
        raise ValueError(f"Too short for header: {len(data)} bytes (need {HEADER_SIZE})")
    magic, version, game_id_bytes, frame_count, _ = struct.unpack_from(HEADER_FMT, data)
    if magic != MAGIC:
        raise ValueError(f"Bad magic: {magic!r} (expected {MAGIC!r})")
    game_id = game_id_bytes.decode("ascii")
    expected_body = frame_count * 2
    actual_body = len(data) - HEADER_SIZE
    if actual_body < expected_body:
        raise ValueError(f"Body truncated: {actual_body} bytes (expected {expected_body})")
    frames = list(struct.unpack_from(f"<{frame_count}H", data, HEADER_SIZE))
    return SpinrecHeader(magic=magic, version=version, game_id=game_id, frame_count=frame_count), frames


def write_spinrec(game_id: str, frames: list[int]) -> bytes:
    """Build a .spinrec binary blob."""
    header = struct.pack(HEADER_FMT, MAGIC, VERSION, game_id.encode("ascii"), len(frames), b"\x00" * 6)
    body = struct.pack(f"<{len(frames)}H", *frames) if frames else b""
    return header + body
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /c/Users/thedo/git/spinlab && python -m pytest tests/test_spinrec.py -v`
Expected: All 7 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/spinrec.py tests/test_spinrec.py
git commit -m "feat: .spinrec binary format reader/writer with tests"
```

---

## Task 2: Lua — `send_event` Wrapper & Encode/Decode Helpers

Refactor existing event sends to use a `send_event` wrapper (prerequisite for source tagging), and add the input bitmask encode/decode functions.

**Files:**
- Modify: `lua/spinlab.lua`

**Context:** Currently there are 5 passive-mode `client:send(to_json(...))` calls for segment events at lines 343, 357, 379, 425, 467. These all need to go through a single `send_event()` function. (Line 480 is an error notification and line 589 is a practice-mode send — leave both as direct `client:send` calls.)

- [ ] **Step 1: Add `send_event` wrapper function**

Insert after the `to_json` function (line 272). The function wraps all event TCP sends:

```lua
local function send_event(event)
  if not client then return end
  if practice.active then return end
  -- Source tagging will be added here in Task 5 (replay mode)
  client:send(to_json(event) .. "\n")
end
```

- [ ] **Step 2: Replace all 5 passive-mode `client:send(to_json(...))` calls with `send_event()`**

Replace event sends at these locations (do NOT replace command responses like `ok:queued`, `pong`, etc.):

- Line 342-344 (`on_level_entrance`): `if client and not practice.active then client:send(to_json(event_data) .. "\n") end` → `send_event(event_data)`
- Line 356-358 (`on_death`): same pattern → `send_event(event_data)`
- Line 378-380 (`on_level_exit`): same pattern → `send_event(event_data)`
- Line 424-426 (`detect_transitions` checkpoint): same pattern → `send_event(event_data)`
- Line 466-468 (`detect_transitions` spawn): same pattern → `send_event(event_data)`

**Leave these as-is** (they are NOT passive segment events):
- Line 480 (`client:send(to_json({event = "error", ...}))`) — error notification when game context is missing
- Line 589 (`client:send(to_json(...))`) — practice-mode attempt result

- [ ] **Step 3: Add input encode/decode functions**

Insert near the `send_event` function:

```lua
-- Input bitmask encoding: matches SNES joypad register layout
local INPUT_BITS = {
  b = 0, y = 1, select = 2, start = 3,
  up = 4, down = 5, left = 6, right = 7,
  a = 8, x = 9, l = 10, r = 11,
}

local function encode_input(tbl)
  local mask = 0
  for name, bit in pairs(INPUT_BITS) do
    if tbl[name] then mask = mask + (1 << bit) end
  end
  return mask
end

local function decode_input(mask)
  local tbl = {}
  for name, bit in pairs(INPUT_BITS) do
    tbl[name] = (mask & (1 << bit)) ~= 0
  end
  return tbl
end
```

- [ ] **Step 4: Test manually in Mesen2**

Load the script in Mesen2, verify the emulator runs without errors. The overlay and passive detection should behave identically to before — pure refactor, no behavior change.

- [ ] **Step 5: Commit**

```bash
git add lua/spinlab.lua
git commit -m "refactor: Lua send_event wrapper + input encode/decode helpers"
```

---

## Task 3: Lua — Recording in Passive Mode

Add the `inputPolled` callback and recording state. When Python sends `reference_start`, Lua begins capturing inputs. On `reference_stop`, Lua flushes to `.spinrec` file.

**Files:**
- Modify: `lua/spinlab.lua`

- [ ] **Step 1: Add recording state variables**

Insert after the `practice_reset()` function (after line 107):

```lua
-- Recording state (passive mode input capture)
local recording = {
  active = false,
  buffer = {},       -- array of uint16 bitmasks
  frame_index = 0,
  output_path = nil, -- .spinrec file path (set by reference_start)
}

local pending_rec_save = nil  -- separate from pending_save to avoid contention
```

- [ ] **Step 2: Add `.spinrec` flush function**

Insert near the file I/O helpers:

```lua
local function flush_spinrec(path, game_id_str, buffer)
  -- Header: SREC (4) + version (2) + game_id (16) + frame_count (4) + reserved (6) = 32
  local f = io.open(path, "wb")
  if not f then
    log("ERROR: Cannot write spinrec: " .. path)
    return false
  end
  -- Magic
  f:write("SREC")
  -- Version (uint16 LE)
  f:write(string.char(1, 0))
  -- Game ID (16 bytes ASCII, pad with zeros if shorter)
  local gid = (game_id_str or ""):sub(1, 16)
  f:write(gid .. string.rep("\0", 16 - #gid))
  -- Frame count (uint32 LE)
  local n = #buffer
  f:write(string.char(n & 0xFF, (n >> 8) & 0xFF, (n >> 16) & 0xFF, (n >> 24) & 0xFF))
  -- Reserved (6 zeros)
  f:write(string.rep("\0", 6))
  -- Body: 2 bytes per frame (uint16 LE)
  for _, mask in ipairs(buffer) do
    f:write(string.char(mask & 0xFF, (mask >> 8) & 0xFF))
  end
  f:close()
  log("Wrote spinrec: " .. path .. " (" .. n .. " frames)")
  return true
end
```

- [ ] **Step 3: Add `inputPolled` callback**

Insert before `on_start_frame`:

```lua
local function on_input_polled()
  if recording.active then
    if recording.frame_index == 0 then
      -- Capture frame 0 save state via dedicated pending variable
      local mss_path = recording.output_path:gsub("%.spinrec$", ".mss")
      pending_rec_save = mss_path
    end
    local input = emu.getInput(0)
    recording.buffer[#recording.buffer + 1] = encode_input(input)
    recording.frame_index = recording.frame_index + 1
  end
end
```

- [ ] **Step 4: Extend `on_cpu_exec` to handle `pending_rec_save`**

In `on_cpu_exec` (line 756), add after the `pending_save` block:

```lua
  if pending_rec_save then
    local path = pending_rec_save
    pending_rec_save = nil
    save_state_to_file(path)
  end
```

- [ ] **Step 5: Add `reference_start` / `reference_stop` TCP handlers**

In `handle_tcp`, inside the JSON event dispatcher (after the `game_context` handler around line 668), add:

```lua
        elseif decoded_event == "reference_start" then
          local path = json_get_str(line, "path")
          if not path or path == "" then
            client:send(to_json({event = "error", message = "reference_start requires path"}) .. "\n")
          else
            recording.active = true
            recording.buffer = {}
            recording.frame_index = 0
            recording.output_path = path
            client:send("ok:recording\n")
            log("Recording started: " .. path)
          end
        elseif decoded_event == "reference_stop" then
          if recording.active then
            recording.active = false
            local path = recording.output_path
            local count = #recording.buffer
            if count > 0 and path then
              flush_spinrec(path, game_id, recording.buffer)
              send_event({event = "rec_saved", path = path, frame_count = count})
            end
            recording.buffer = {}
            recording.frame_index = 0
            recording.output_path = nil
            client:send("ok:stopped\n")
            log("Recording stopped: " .. count .. " frames")
          else
            client:send("ok:not_recording\n")
          end
```

- [ ] **Step 6: Add recording cleanup to disconnect handlers**

In both disconnect cleanup blocks (heartbeat fail at line ~643, receive error at line ~727), add after the practice cleanup:

```lua
        if recording.active then
          recording.active = false
          recording.buffer = {}
          recording.frame_index = 0
          -- Delete partial .mss if it exists
          if recording.output_path then
            local mss = recording.output_path:gsub("%.spinrec$", ".mss")
            os.remove(mss)
          end
          recording.output_path = nil
          log("Recording auto-cleared on disconnect")
        end
```

- [ ] **Step 7: Register `inputPolled` callback**

At the bottom of the script, after the `startFrame` registration (line 807):

```lua
emu.addEventCallback(on_input_polled, emu.eventType.inputPolled)
```

- [ ] **Step 8: Test manually in Mesen2**

1. Start Mesen2 with script loaded
2. Connect via TCP, send `{"event": "reference_start", "path": "<data_dir>/test.spinrec"}`
3. Play through a level
4. Send `{"event": "reference_stop"}`
5. Verify: `test.spinrec` and `test.mss` files exist. Check file size makes sense (header + 2 bytes × frames played).

- [ ] **Step 9: Commit**

```bash
git add lua/spinlab.lua
git commit -m "feat: Lua input recording in passive mode (.spinrec format)"
```

---

## Task 4: Python — Extend SessionManager for Recording

Extend `start_reference()` and `stop_reference()` to send TCP commands and handle the `rec_saved` event.

**Files:**
- Modify: `python/spinlab/session_manager.py`
- Modify: `tests/test_session_manager.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_session_manager.py`:

```python
class TestRecording:
    @pytest.mark.asyncio
    async def test_start_reference_sends_tcp_command(self, tmp_path):
        """start_reference sends reference_start with .spinrec path to Lua."""
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=tmp_path, default_category="any%")
        sm.game_id = "abcdef0123456789"
        sm.game_name = "Test Game"

        result = await sm.start_reference()
        assert result["status"] == "started"
        assert sm.mode == "reference"

        # Verify TCP command was sent with path
        tcp.send.assert_called()
        sent = tcp.send.call_args_list[-1][0][0]
        import json
        msg = json.loads(sent)
        assert msg["event"] == "reference_start"
        assert msg["path"].endswith(".spinrec")

    @pytest.mark.asyncio
    async def test_stop_reference_sends_tcp_command(self, tmp_path):
        """stop_reference sends reference_stop to Lua."""
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=tmp_path, default_category="any%")
        sm.game_id = "abcdef0123456789"
        sm.game_name = "Test Game"
        await sm.start_reference()
        tcp.send.reset_mock()

        result = await sm.stop_reference()
        assert result["status"] == "stopped"

        tcp.send.assert_called_once()
        import json
        msg = json.loads(tcp.send.call_args[0][0])
        assert msg["event"] == "reference_stop"

    @pytest.mark.asyncio
    async def test_rec_saved_event_stores_path(self, tmp_path):
        """rec_saved event from Lua stores .spinrec path on session."""
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=tmp_path, default_category="any%")
        sm.game_id = "abcdef0123456789"
        sm.game_name = "Test Game"
        await sm.start_reference()

        await sm.route_event({"event": "rec_saved", "path": "/data/test.spinrec", "frame_count": 1000})
        assert sm.rec_path == "/data/test.spinrec"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /c/Users/thedo/git/spinlab && python -m pytest tests/test_session_manager.py::TestRecording -v`
Expected: FAIL — `reference_start` doesn't send TCP, `rec_path` doesn't exist.

- [ ] **Step 3: Implement changes to SessionManager**

In `session_manager.py`:

1. Add `rec_path` to `__init__` (around line 44):
```python
self.rec_path: str | None = None
```

2. Add `rec_path` reset to `_clear_ref_state()` (line 130):
```python
self.rec_path = None
```

3. Extend `start_reference()` (line 456):
- Add guard for replay mode (currently only checks practice):
```python
    if self.mode in ("practice", "replay"):
        return {"status": f"{self.mode}_active"}
```
- After `self.mode = "reference"` (line 469), send TCP command with `.spinrec` path:
```python
    rec_path = str(self._game_rec_dir() / f"{run_id}.spinrec")
    await self.tcp.send(json.dumps({"event": "reference_start", "path": rec_path}))
```

4. Extend `stop_reference()` (line 473) to send TCP command before clearing state:
```python
    # Before self._clear_ref_state() (line 477):
    if self.tcp.is_connected:
        await self.tcp.send(json.dumps({"event": "reference_stop"}))
```

5. Add `rec_saved` handler in `route_event()` (around line 248):
```python
        elif evt_type == "rec_saved":
            self.rec_path = event.get("path")
```

6. Add `data_dir` to `SessionManager.__init__()` and `_game_rec_dir()` helper.

The `data_dir` comes from `config["data"]["dir"]` in `cli.py` (line 47). Currently it's NOT passed to SessionManager — only used for the DB path. Thread it through:

In `dashboard.py`'s `create_app()`, extract `data_dir` from config and pass to SessionManager:
```python
data_dir = Path(config.get("data", {}).get("dir", "data"))
session = SessionManager(db, tcp, rom_dir, default_category, data_dir=data_dir)
```

In `session_manager.py`'s `__init__()`, add `data_dir` parameter:
```python
def __init__(self, db, tcp, rom_dir, default_category="any%", data_dir: Path | None = None):
    ...
    self.data_dir = data_dir or Path("data")
```

Add the helper:
```python
def _game_rec_dir(self) -> Path:
    """Return the per-game recording directory, creating it if needed."""
    d = self.data_dir / (self.game_id or "unknown") / "rec"
    d.mkdir(parents=True, exist_ok=True)
    return d
```

Then update the `start_reference()` TCP send (item 3 above) to use it:
```python
rec_path = str(self._game_rec_dir() / f"{run_id}.spinrec")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /c/Users/thedo/git/spinlab && python -m pytest tests/test_session_manager.py::TestRecording -v`
Expected: All 3 tests PASS.

- [ ] **Step 5: Run full test suite to check for regressions**

Run: `cd /c/Users/thedo/git/spinlab && python -m pytest tests/ -v`
Expected: All existing tests still PASS.

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/session_manager.py tests/test_session_manager.py
git commit -m "feat: SessionManager sends reference_start/stop TCP commands, handles rec_saved"
```

---

## Task 5: Lua — Replay Mode

Add the replay state machine: load `.spinrec` + `.mss`, inject inputs via `inputPolled`, report progress, source-tag events.

**Files:**
- Modify: `lua/spinlab.lua`

- [ ] **Step 1: Add replay state variables**

Insert after the recording state block:

```lua
-- Replay state
local replay = {
  active = false,
  frames = {},        -- array of uint16 bitmasks loaded from .spinrec
  index = 1,          -- current frame position
  total = 0,          -- total frames
  path = nil,         -- .spinrec file path
  speed = 0,          -- 0 = max, 100 = normal
  prev_speed = nil,   -- speed to restore after replay
  last_progress_ms = 0,  -- wall-clock time of last progress event
}
```

- [ ] **Step 2: Add `.spinrec` reader function**

```lua
local function read_spinrec(path)
  local f = io.open(path, "rb")
  if not f then return nil, "file not found: " .. path end
  local data = f:read("*a")
  f:close()
  if #data < 32 then return nil, "file too short" end
  -- Validate magic
  if data:sub(1, 4) ~= "SREC" then return nil, "bad magic" end
  -- Parse header
  local b = function(i) return data:byte(i) end
  local frame_count = b(23) + b(24) * 256 + b(25) * 65536 + b(26) * 16777216
  local gid = data:sub(7, 22)
  -- Validate body length
  local expected_body = frame_count * 2
  if #data - 32 < expected_body then return nil, "body truncated" end
  -- Parse frames
  local frames = {}
  for i = 1, frame_count do
    local offset = 32 + (i - 1) * 2
    frames[i] = b(offset + 1) + b(offset + 2) * 256
  end
  return {game_id = gid, frame_count = frame_count, frames = frames}, nil
end
```

- [ ] **Step 3: Add source tagging to `send_event`**

Update the `send_event` function from Task 2:

```lua
local function send_event(event)
  if not client then return end
  if practice.active then return end
  if replay.active then
    event.source = "replay"
  end
  client:send(to_json(event) .. "\n")
end
```

- [ ] **Step 4: Extend `on_input_polled` for replay injection**

Update the `inputPolled` callback:

```lua
local function on_input_polled()
  if recording.active then
    if recording.frame_index == 0 then
      local mss_path = recording.output_path:gsub("%.spinrec$", ".mss")
      pending_rec_save = mss_path
    end
    local input = emu.getInput(0)
    recording.buffer[#recording.buffer + 1] = encode_input(input)
    recording.frame_index = recording.frame_index + 1
  elseif replay.active and replay.index <= replay.total then
    emu.setInput(0, decode_input(replay.frames[replay.index]))
    replay.index = replay.index + 1
    -- Progress reporting (wall-clock throttled)
    local now = os.clock() * 1000
    if now - replay.last_progress_ms >= 100 then
      replay.last_progress_ms = now
      send_event({event = "replay_progress", frame = replay.index - 1, total = replay.total})
    end
    -- Check if replay finished
    if replay.index > replay.total then
      send_event({event = "replay_finished", path = replay.path, frames_played = replay.total})
      -- Restore speed
      if replay.prev_speed then
        emu.setSpeed(replay.prev_speed)
      end
      replay.active = false
      replay.frames = {}
      replay.index = 1
      replay.path = nil
      log("Replay finished")
    end
  end
end
```

**Note on `emu.setInput` signature:** The plan uses `emu.setInput(0, decoded_table)` (port first). This matches Mesen2's typical API convention but is a **PoC validation item** — the exact signature must be confirmed during manual testing in Step 8. If the order is wrong, swap arguments in `on_input_polled`.

- [ ] **Step 5: Add `replay` and `replay_stop` TCP handlers**

In `handle_tcp`, inside the JSON event dispatcher:

```lua
        elseif decoded_event == "replay" then
          if practice.active or recording.active then
            client:send(to_json({event = "replay_error", message = "cannot replay during practice or recording"}) .. "\n")
          else
            local path = json_get_str(line, "path")
            local speed = json_get_num(line, "speed") or 0
            if not path then
              client:send(to_json({event = "replay_error", message = "replay requires path"}) .. "\n")
            else
              local rec, read_err = read_spinrec(path)
              if not rec then
                client:send(to_json({event = "replay_error", message = read_err}) .. "\n")
              elseif game_id and rec.game_id:gsub("%z+$", "") ~= game_id then
                client:send(to_json({event = "replay_error", message = "game_id mismatch"}) .. "\n")
              else
                replay.frames = rec.frames
                replay.total = rec.frame_count
                replay.index = 1
                replay.path = path
                replay.speed = speed
                replay.last_progress_ms = os.clock() * 1000
                -- Load companion .mss
                local mss_path = path:gsub("%.spinrec$", ".mss")
                pending_load = mss_path
                -- Set speed (PoC validation: confirm emu.setSpeed exists)
                replay.prev_speed = 100  -- assume normal speed was active
                if emu.setSpeed then
                  emu.setSpeed(speed)
                end
                replay.active = true
                client:send(to_json({event = "replay_started", path = path, frame_count = rec.frame_count}) .. "\n")
                log("Replay started: " .. path .. " (" .. rec.frame_count .. " frames, speed=" .. speed .. ")")
              end
            end
          end
        elseif decoded_event == "replay_stop" then
          if replay.active then
            replay.active = false
            replay.frames = {}
            replay.index = 1
            if replay.prev_speed and emu.setSpeed then
              emu.setSpeed(replay.prev_speed)
            end
            replay.path = nil
            client:send("ok:replay_stopped\n")
            log("Replay stopped by command")
          else
            client:send("ok:not_replaying\n")
          end
```

- [ ] **Step 6: Verify `json_get_num` exists (line 173 — no action needed)**

`json_get_num` already exists at line 173 of `spinlab.lua`. No implementation needed. Note: it only matches integers (`%d+`). If fractional speed values are ever needed, update the pattern — but for now `0` and `100` are fine.

- [ ] **Step 7: Add replay cleanup to disconnect handlers**

In both disconnect cleanup blocks, add:

```lua
        if replay.active then
          replay.active = false
          replay.frames = {}
          replay.index = 1
          if replay.prev_speed and emu.setSpeed then
            emu.setSpeed(replay.prev_speed)
          end
          replay.path = nil
          log("Replay auto-cleared on disconnect")
        end
```

- [ ] **Step 8: Test manually in Mesen2**

1. First, create a `.spinrec` from Task 3's recording test
2. Send `{"event": "replay", "path": "<path_to_spinrec>", "speed": 100}` (normal speed first to watch)
3. Verify: emulator loads the save state and replays the inputs. Mario moves identically to the recording.
4. Verify: segment events fire with `"source": "replay"` tag
5. Verify: `replay_progress` events arrive periodically
6. Verify: `replay_finished` arrives when done
7. Test with `"speed": 0` — should replay at maximum speed
8. **Validate `emu.setInput` and `emu.setSpeed` signatures** — adjust argument order if needed

- [ ] **Step 9: Commit**

```bash
git add lua/spinlab.lua
git commit -m "feat: Lua replay mode — loads .spinrec, injects inputs, source-tags events"
```

---

## Task 6: Python — SessionManager Replay Orchestration

Add `start_replay()` and `stop_replay()` to SessionManager, handle replay events, source tagging for elapsed times.

**Files:**
- Modify: `python/spinlab/session_manager.py`
- Create: `tests/test_replay.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_replay.py
"""Tests for SessionManager replay orchestration."""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from spinlab.session_manager import SessionManager


def make_mock_tcp():
    tcp = MagicMock()
    tcp.is_connected = True
    tcp.send = AsyncMock()
    tcp.recv_event = AsyncMock(return_value=None)
    return tcp


def make_mock_db():
    db = MagicMock()
    db.upsert_game = MagicMock()
    db.create_session = MagicMock()
    db.end_session = MagicMock()
    db.create_capture_run = MagicMock()
    db.set_active_capture_run = MagicMock()
    db.get_recent_attempts = MagicMock(return_value=[])
    db.get_all_segments_with_model = MagicMock(return_value=[])
    db.load_model_state = MagicMock(return_value=None)
    db.load_allocator_config = MagicMock(return_value=None)
    db.upsert_segment = MagicMock()
    db.add_variant = MagicMock()
    db.get_active_segments = MagicMock(return_value=[])
    return db


class TestStartReplay:
    @pytest.mark.asyncio
    async def test_sends_replay_command(self, tmp_path):
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=tmp_path, default_category="any%")
        sm.game_id = "abcdef0123456789"
        sm.game_name = "Test Game"

        result = await sm.start_replay("/data/test.spinrec", speed=0)
        assert result["status"] == "started"
        assert sm.mode == "replay"

        msg = json.loads(tcp.send.call_args[0][0])
        assert msg["event"] == "replay"
        assert msg["path"] == "/data/test.spinrec"
        assert msg["speed"] == 0

    @pytest.mark.asyncio
    async def test_rejects_during_practice(self, tmp_path):
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=tmp_path, default_category="any%")
        sm.game_id = "abcdef0123456789"
        sm.mode = "practice"

        result = await sm.start_replay("/data/test.spinrec")
        assert result["status"] == "practice_active"

    @pytest.mark.asyncio
    async def test_rejects_during_reference(self, tmp_path):
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=tmp_path, default_category="any%")
        sm.game_id = "abcdef0123456789"
        sm.mode = "reference"

        result = await sm.start_replay("/data/test.spinrec")
        assert result["status"] == "reference_active"


class TestStopReplay:
    @pytest.mark.asyncio
    async def test_sends_stop_command(self, tmp_path):
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=tmp_path, default_category="any%")
        sm.game_id = "abcdef0123456789"
        sm.game_name = "Test Game"
        sm.mode = "replay"

        result = await sm.stop_replay()
        assert result["status"] == "stopped"
        assert sm.mode == "idle"

        msg = json.loads(tcp.send.call_args[0][0])
        assert msg["event"] == "replay_stop"


class TestReplayEvents:
    @pytest.mark.asyncio
    async def test_replay_finished_returns_to_idle(self, tmp_path):
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=tmp_path, default_category="any%")
        sm.game_id = "abcdef0123456789"
        sm.game_name = "Test Game"
        sm.mode = "replay"

        await sm.route_event({"event": "replay_finished", "path": "/data/test.spinrec", "frames_played": 5000})
        assert sm.mode == "idle"

    @pytest.mark.asyncio
    async def test_replay_error_returns_to_idle(self, tmp_path):
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=tmp_path, default_category="any%")
        sm.game_id = "abcdef0123456789"
        sm.mode = "replay"

        await sm.route_event({"event": "replay_error", "message": "game_id mismatch"})
        assert sm.mode == "idle"

    @pytest.mark.asyncio
    async def test_replay_events_still_capture_segments(self, tmp_path):
        """Events with source=replay still flow through reference capture pipeline."""
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=tmp_path, default_category="any%")
        sm.game_id = "abcdef0123456789"
        sm.game_name = "Test Game"
        sm.mode = "replay"
        sm.ref_capture_run_id = "replay_test123"

        # Simulate a level entrance during replay
        await sm.route_event({
            "event": "level_entrance",
            "level_num": 0x105,
            "room": 0,
            "frame": 100,
            "ts_ms": 1000,
            "session": "passive",
            "state_path": "/data/test.mss",
            "source": "replay",
        })
        # Reference capture works during replay — segments are created
        assert sm.ref_pending_start is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /c/Users/thedo/git/spinlab && python -m pytest tests/test_replay.py -v`
Expected: FAIL — `start_replay` and `stop_replay` don't exist.

- [ ] **Step 3: Implement replay methods in SessionManager**

Add to `session_manager.py`:

```python
    # --- Replay mode ---
    async def start_replay(self, spinrec_path: str, speed: int = 0) -> dict:
        """Begin replay of a .spinrec file."""
        if self.mode == "practice":
            return {"status": "practice_active"}
        if self.mode == "reference":
            return {"status": "reference_active"}
        if self.mode == "replay":
            return {"status": "already_replaying"}
        if not self.tcp.is_connected:
            return {"status": "not_connected"}

        # Set up reference capture so replayed events create segments
        gid = self._require_game()
        self._clear_ref_state()
        run_id = f"replay_{uuid.uuid4().hex[:8]}"
        name = f"Replay {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M')}"
        self.db.create_capture_run(run_id, gid, name)
        self.db.set_active_capture_run(run_id)
        self.ref_capture_run_id = run_id

        self.mode = "replay"
        await self.tcp.send(json.dumps({"event": "replay", "path": spinrec_path, "speed": speed}))
        await self._notify_sse()
        return {"status": "started", "run_id": run_id}

    async def stop_replay(self) -> dict:
        """Abort replay."""
        if self.mode != "replay":
            return {"status": "not_replaying"}
        if self.tcp.is_connected:
            await self.tcp.send(json.dumps({"event": "replay_stop"}))
        self._clear_ref_state()
        await self._notify_sse()
        return {"status": "stopped"}
```

**Note on scheduler exclusion:** The spec says "the scheduler ignores replay-sourced segments for difficulty modeling." This is NOT implemented in this plan because the scheduler currently only looks at `Attempt` records (practice results), not reference run segments. Reference segments don't feed the scheduler regardless of source. If this changes in the future, add a `source` filter to `Attempt` records.

Add replay event routing in `route_event()`:

```python
        elif evt_type == "replay_started":
            await self._notify_sse()
        elif evt_type == "replay_progress":
            await self._notify_sse()
        elif evt_type == "replay_finished":
            self._clear_ref_state()
            await self._notify_sse()
        elif evt_type == "replay_error":
            self._clear_ref_state()
            await self._notify_sse()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /c/Users/thedo/git/spinlab && python -m pytest tests/test_replay.py -v`
Expected: All tests PASS.

- [ ] **Step 5: Run full test suite**

Run: `cd /c/Users/thedo/git/spinlab && python -m pytest tests/ -v`
Expected: All tests PASS.

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/session_manager.py tests/test_replay.py
git commit -m "feat: SessionManager replay orchestration — start/stop/event handling"
```

---

## Task 7: Dashboard API + CLI

Add REST endpoints and CLI command for replay.

**Files:**
- Modify: `python/spinlab/dashboard.py`
- Modify: `python/spinlab/cli.py`

- [ ] **Step 1: Add dashboard endpoints**

In `dashboard.py`, after the reference endpoints:

```python
    @app.post("/api/replay/start")
    async def replay_start(req: Request):
        body = await req.json()
        path = body.get("path")
        speed = body.get("speed", 0)
        if not path:
            raise HTTPException(status_code=400, detail="path required")
        return await session.start_replay(path, speed=speed)

    @app.post("/api/replay/stop")
    async def replay_stop():
        return await session.stop_replay()
```

- [ ] **Step 2: Add `replay` CLI command**

In `cli.py`, add a new subparser:

```python
    replay_p = sub.add_parser("replay", help="Replay a .spinrec file to regenerate a reference run")
    replay_p.add_argument("path", help="Path to .spinrec file")
    replay_p.add_argument("--speed", type=int, default=0, help="Emulation speed (0=max, 100=normal)")
```

Add the handler:

```python
    elif args.command == "replay":
        import requests
        resp = requests.post(
            f"http://127.0.0.1:{args.port}/api/replay/start",
            json={"path": args.path, "speed": args.speed},
        )
        print(resp.json())
```

- [ ] **Step 3: Add `replay` to `get_state()` output**

In `session_manager.py`'s `get_state()` method, include replay info when mode is "replay":

```python
    if self.mode == "replay":
        state["replay"] = {"rec_path": getattr(self, "_replay_path", None)}
```

- [ ] **Step 4: Test the dashboard endpoints**

Run: `cd /c/Users/thedo/git/spinlab && python -m pytest tests/test_dashboard.py -v`
Expected: Existing tests PASS. (New endpoints are integration-tested manually with Mesen2.)

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/dashboard.py python/spinlab/cli.py python/spinlab/session_manager.py
git commit -m "feat: dashboard /api/replay endpoints + CLI replay command"
```

---

## Task 8: End-to-End Manual Validation

Full flow test with Mesen2. This is the critical validation step.

**Files:** None (manual testing)

- [ ] **Step 1: Record a reference run**

1. `spinlab dashboard` — start dashboard
2. Open Mesen2 with a short ROM
3. Dashboard → Start Reference
4. Play through 1-2 levels
5. Dashboard → Stop Reference
6. Verify: `.spinrec` + `.mss` files exist in `{data_dir}/{game_id}/rec/`
7. Verify: segments were captured normally

- [ ] **Step 2: Replay at normal speed**

1. Dashboard → Manage tab → find the reference run → Replay button (or CLI: `spinlab replay <path> --speed 100`)
2. Watch: emulator should replay the exact same inputs
3. Verify: segment events fire with `"source": "replay"` in dashboard logs
4. Verify: `replay_progress` SSE events show in browser console
5. Verify: `replay_finished` fires and mode returns to idle

- [ ] **Step 3: Replay at max speed**

1. CLI: `spinlab replay <path> --speed 0`
2. Verify: emulator runs at maximum speed
3. Verify: replay finishes quickly
4. Verify: all segments re-created correctly

- [ ] **Step 4: Validate `emu.setInput` / `emu.setSpeed` API signatures**

If either function has a different signature than expected, fix the Lua code and commit the fix:

```bash
git add lua/spinlab.lua
git commit -m "fix: correct emu.setInput/setSpeed argument order for Mesen2"
```

- [ ] **Step 5: Create a short test fixture**

1. Record a minimal run (1 level entrance + goal)
2. Copy the `.spinrec` + `.mss` to `tests/fixtures/rec/`
3. Commit:

```bash
git add tests/fixtures/rec/
git commit -m "test: add short .spinrec fixture for integration tests"
```
