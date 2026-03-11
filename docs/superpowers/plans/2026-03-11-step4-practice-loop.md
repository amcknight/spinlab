# Step 4 — Practice Loop MVP Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire the existing DB/scheduler/Lua pieces into a working practice loop — Python orchestrator drives splits via TCP, Lua handles state loading, auto-retry on death, overlay, and rating input.

**Architecture:** Python (`orchestrator.py`) connects to Mesen2's Lua TCP server, seeds the DB from a manifest, then loops: pick next split → send `practice_load:<json>` → wait for `attempt_result` → log attempt → update schedule. Lua manages the inner loop: load state, watch for death (auto-reload) or clear (show rating prompt), collect L+D-pad rating, push result back to Python.

**Tech Stack:** Python 3.11, SQLite (via existing `db.py`), LuaSocket (Mesen2 built-in), `pyyaml`, `pytest`

---

## File Structure

| File | Role |
|------|------|
| `python/spinlab/orchestrator.py` | New — manifest seeding, TCP client, main loop |
| `tests/test_orchestrator.py` | New — unit tests for parseable functions |
| `lua/spinlab.lua` | Modify — add practice mode state machine, TCP commands, overlay |

No changes to `db.py`, `scheduler.py`, `models.py`, `capture.py`.

---

## Chunk 1: Python Orchestrator

### Task 1: `_parse_attempt_result_from_buffer` (testable core of TCP receive)

The only function in the orchestrator that warrants a unit test — it handles TCP buffering
and JSON parsing. Keep it a module-level function so tests can import it directly.

**Files:**
- Create: `python/spinlab/orchestrator.py`
- Create: `tests/test_orchestrator.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_orchestrator.py
import json
import pytest
from spinlab.orchestrator import _parse_attempt_result_from_buffer


GOOD_RESULT = {
    "event": "attempt_result",
    "split_id": "smw_cod:5:0:normal",
    "completed": True,
    "time_ms": 11234,
    "goal": "normal",
    "rating": "good",
}


def test_parses_complete_line():
    buf = json.dumps(GOOD_RESULT) + "\n"
    result, remaining = _parse_attempt_result_from_buffer(buf)
    assert result == GOOD_RESULT
    assert remaining == ""


def test_returns_none_for_incomplete_line():
    buf = json.dumps(GOOD_RESULT)  # no newline
    result, remaining = _parse_attempt_result_from_buffer(buf)
    assert result is None
    assert remaining == buf


def test_discards_non_attempt_result_lines():
    buf = "ok:queued\npong\n" + json.dumps(GOOD_RESULT) + "\n"
    result, remaining = _parse_attempt_result_from_buffer(buf)
    assert result == GOOD_RESULT
    assert remaining == ""


def test_discards_malformed_json():
    buf = "this is not json\n" + json.dumps(GOOD_RESULT) + "\n"
    result, remaining = _parse_attempt_result_from_buffer(buf)
    assert result == GOOD_RESULT
    assert remaining == ""


def test_discards_json_without_attempt_result_event():
    other = json.dumps({"event": "something_else", "data": 1}) + "\n"
    buf = other + json.dumps(GOOD_RESULT) + "\n"
    result, remaining = _parse_attempt_result_from_buffer(buf)
    assert result == GOOD_RESULT


def test_leaves_partial_second_message_in_buffer():
    partial = '{"event": "attempt_result"'  # incomplete second message
    buf = json.dumps(GOOD_RESULT) + "\n" + partial
    result, remaining = _parse_attempt_result_from_buffer(buf)
    assert result == GOOD_RESULT
    assert remaining == partial
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/test_orchestrator.py -v
```

Expected: `ModuleNotFoundError: No module named 'spinlab.orchestrator'`

- [ ] **Step 3: Create `orchestrator.py` with just the buffer parsing function**

```python
# python/spinlab/orchestrator.py
"""SpinLab practice session orchestrator."""
from __future__ import annotations

import json
from typing import Optional


def _parse_attempt_result_from_buffer(buf: str) -> tuple[Optional[dict], str]:
    """Parse one attempt_result JSON event from the buffer.

    Returns (result_dict, remaining_buf) if found, or (None, buf) if not enough data.
    Discards non-JSON lines and JSON lines that aren't attempt_result events.
    """
    while "\n" in buf:
        line, buf = buf.split("\n", 1)
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
            if msg.get("event") == "attempt_result":
                return msg, buf
        except json.JSONDecodeError:
            pass  # discard plain-text responses like ok:queued, pong
    return None, buf
```

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/test_orchestrator.py -v
```

Expected: all 6 tests PASS

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/orchestrator.py tests/test_orchestrator.py
git commit -m "feat(orchestrator): add TCP buffer parser with tests"
```

---

### Task 2: Manifest seeding helpers

Loads the latest manifest YAML and upserts game + splits into the DB.

**Files:**
- Modify: `python/spinlab/orchestrator.py`

- [ ] **Step 1: Add the seeding functions (no new tests needed — DB layer is already tested)**

Add to `orchestrator.py` after the existing imports/function:

```python
import os
import sys
from pathlib import Path

import yaml

from .db import Database
from .models import Split


def find_latest_manifest(data_dir: Path) -> Optional[Path]:
    """Return the most-recently-named manifest YAML, or None if none exist."""
    captures = list((data_dir / "captures").glob("*_manifest.yaml"))
    if not captures:
        return None
    return sorted(captures)[-1]  # date-prefixed filenames sort correctly


def load_manifest(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def seed_db_from_manifest(db: Database, manifest: dict, game_name: str) -> None:
    """Upsert game + all splits from manifest into the DB.

    Does NOT create schedule entries — that is Scheduler.init_schedules()'s job,
    called separately in run() after seeding.
    """
    game_id: str = manifest["game_id"]
    category: str = manifest.get("category", "any%")
    db.upsert_game(game_id, game_name, category)

    for entry in manifest["splits"]:
        split = Split(
            id=entry["id"],
            game_id=game_id,
            level_number=entry["level_number"],
            room_id=entry.get("room_id"),
            goal=entry["goal"],
            state_path=entry.get("state_path"),
            reference_time_ms=entry.get("reference_time_ms"),
        )
        db.upsert_split(split)
```

- [ ] **Step 2: Run existing tests to confirm nothing broken**

```
pytest -v
```

Expected: all tests PASS

- [ ] **Step 3: Commit**

```bash
git add python/spinlab/orchestrator.py
git commit -m "feat(orchestrator): add manifest loading and DB seeding"
```

---

### Task 3: TCP helpers and full orchestrator main loop

**Files:**
- Modify: `python/spinlab/orchestrator.py`

- [ ] **Step 1: Add TCP connect helper and `recv_until_attempt_result`**

Add to `orchestrator.py`:

```python
import socket
import time


def connect_to_lua(host: str, port: int, timeout: float = 30.0) -> socket.socket:
    """Connect to Lua TCP server, retrying every 0.5s until timeout."""
    deadline = time.monotonic() + timeout
    last_err: Optional[Exception] = None
    while time.monotonic() < deadline:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.connect((host, port))
            return sock
        except OSError as e:
            last_err = e
            sock.close()
            time.sleep(0.5)
    raise ConnectionError(f"Could not connect to Lua on {host}:{port}") from last_err


def send_line(sock: socket.socket, msg: str) -> None:
    sock.sendall((msg + "\n").encode("utf-8"))


def recv_until_attempt_result(sock: socket.socket) -> dict:
    """Block until Lua pushes an attempt_result event. No timeout."""
    buf = ""
    while True:
        chunk = sock.recv(4096).decode("utf-8")
        if not chunk:
            raise ConnectionError("TCP socket closed while waiting for attempt_result")
        buf += chunk
        result, buf = _parse_attempt_result_from_buffer(buf)
        if result is not None:
            return result
```

- [ ] **Step 2: Add the `run()` function and `__main__` entry point**

Add to `orchestrator.py`:

```python
import uuid

from .models import Attempt, Rating, SplitCommand
from .scheduler import Scheduler


def run(config_path: Path = Path("config.yaml")) -> None:
    # -- Config --
    with config_path.open(encoding="utf-8") as f:
        config = yaml.safe_load(f)

    game_id: str = config["game"]["id"]
    game_name: str = config["game"]["name"]
    host: str = config["network"]["host"]
    port: int = config["network"]["port"]
    base_interval: float = float(config["scheduler"]["base_interval_minutes"])
    data_dir = Path(config["data"]["dir"])

    # -- Manifest → DB --
    manifest_path = find_latest_manifest(data_dir)
    if not manifest_path:
        sys.exit(f"No manifest found in {data_dir / 'captures'} — run capture first.")

    db = Database(data_dir / "spinlab.db")
    manifest = load_manifest(manifest_path)
    seed_db_from_manifest(db, manifest, game_name)

    scheduler = Scheduler(db, game_id, base_interval)
    scheduler.init_schedules()

    if not db.get_active_splits(game_id):
        sys.exit("No active splits in DB — check manifest.")

    # -- Connect --
    print(f"Connecting to Lua on {host}:{port} (waiting up to 30s)...")
    sock = connect_to_lua(host, port)

    # Ping to verify connection
    send_line(sock, "ping")
    buf = ""
    while "pong" not in buf:
        buf += sock.recv(256).decode("utf-8")
    print("Connected.")

    # -- Session --
    session_id = uuid.uuid4().hex
    db.create_session(session_id, game_id)
    splits_attempted = 0
    splits_completed = 0
    # Track splits skipped this session due to missing state files.
    # If every active split ends up skipped, exit rather than infinite-loop.
    session_skipped: set[str] = set()

    try:
        while True:
            cmd = scheduler.pick_next()
            if cmd is None:
                print("No splits available — exiting.")
                break

            if cmd.state_path and not os.path.exists(cmd.state_path):
                session_skipped.add(cmd.id)
                print(f"[warn] Missing state file: {cmd.state_path} — skipping {cmd.id}")
                active = db.get_active_splits(game_id)
                if all(s.id in session_skipped for s in active):
                    sys.exit("All splits have missing state files — exiting.")
                continue

            send_line(sock, "practice_load:" + json.dumps(cmd.to_dict()))
            result = recv_until_attempt_result(sock)

            rating = Rating(result["rating"])
            attempt = Attempt(
                split_id=result["split_id"],
                session_id=session_id,
                completed=result["completed"],
                time_ms=result.get("time_ms"),
                goal_matched=(result.get("goal") == cmd.goal) if result.get("completed") else None,
                rating=rating,
                source="practice",
            )
            db.log_attempt(attempt)
            scheduler.process_rating(result["split_id"], rating)

            splits_attempted += 1
            if result["completed"]:
                splits_completed += 1

            status = "✓" if result["completed"] else "✗"
            print(f"{status} {result['split_id']}  {rating.value}  "
                  f"{result.get('time_ms', '?')}ms")

    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        try:
            send_line(sock, "practice_stop")
            sock.recv(64)  # drain ok response
        except OSError:
            pass
        sock.close()
        db.end_session(session_id, splits_attempted, splits_completed)
        db.close()
        print(f"Session ended: {splits_attempted} attempts, {splits_completed} completed.")


if __name__ == "__main__":
    run()
```

- [ ] **Step 3: Consolidate all imports to the top of `orchestrator.py`**

The file has been built incrementally and will have mid-file import statements. Before
committing, move ALL imports to the top in PEP 8 order (stdlib → third-party → local):

```python
# python/spinlab/orchestrator.py
"""SpinLab practice session orchestrator."""
from __future__ import annotations

import json
import os
import socket
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

import yaml

from .db import Database
from .models import Attempt, Rating, Split
from .scheduler import Scheduler
```

Remove any duplicate import statements scattered through the file.

- [ ] **Step 4: Run all tests to confirm nothing broken**

```
pytest -v
```

Expected: all tests PASS

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/orchestrator.py
git commit -m "feat(orchestrator): add TCP helpers and main practice loop"
```

---

## Chunk 2: Lua Practice Mode

All Lua tasks are manually tested in Mesen2 — no automated tests. The Lua file to edit
is `lua/spinlab.lua`.

**Key API facts confirmed from `emu_probe.txt`:**
- `emu.breakExecution()` exists, `emu.resume()` exists — BUT calling `breakExecution()`
  from a `startFrame` callback halts all callbacks, so per-frame controller polling
  won't fire while paused. **Do not use pause/resume.** RATING state runs every frame
  until input arrives (game continues its fanfare animation, but we load a new state
  immediately after rating, so it doesn't matter).
- `emu.getInput(port)` exists — use `emu.getInput(0)` for port 1 controller.
- `emu.drawString`, `emu.measureString`, `emu.drawRectangle` all exist.

---

### Task 4: Controller input probe + practice state variables

Before building the full state machine, verify that `emu.getInput(0)` returns the
expected SNES button fields.

**Files:**
- Modify: `lua/spinlab.lua`

- [ ] **Step 1: Add a temporary controller probe to `check_keyboard()`**

In `check_keyboard()`, add temporarily (remove after verifying):

```lua
local function check_keyboard()
  if key_just_pressed("T") then pending_save = TEST_STATE_FILE end
  if key_just_pressed("Y") then pending_load = TEST_STATE_FILE end

  -- TEMP PROBE: press P to log controller state
  if key_just_pressed("P") then
    local inp = emu.getInput(0)
    if inp then
      for k, v in pairs(inp) do
        log("INPUT: " .. tostring(k) .. " = " .. tostring(v))
      end
    else
      log("INPUT: emu.getInput(0) returned nil")
    end
  end
end
```

- [ ] **Step 2: Load the script in Mesen2, press P, check emu.log**

In Mesen2: Script Window → Run. Then hold an SNES button and press keyboard P.
Verify the log shows field names like `l`, `up`, `down`, `left`, `right`, etc.

Expected output (example):
```
[SpinLab] INPUT: l = false
[SpinLab] INPUT: r = false
[SpinLab] INPUT: up = false
[SpinLab] INPUT: down = true
...
```

If the output is different (e.g., numeric keys, different names), note the actual field
names and update all L+D-pad checks in the steps below accordingly.

- [ ] **Step 3: Remove the probe, add practice state variables to the STATE section**

Remove the probe code. Add to the `-- STATE --` section (after the existing state vars):

```lua
-- Practice mode state
local PSTATE_IDLE    = "idle"
local PSTATE_LOADING = "loading"
local PSTATE_PLAYING = "playing"
local PSTATE_RATING  = "rating"

local practice_mode       = false   -- true while in practice mode
local practice_state      = PSTATE_IDLE
local practice_split      = nil     -- current split info table
local practice_start_ms   = 0       -- ts_ms() when current attempt started
local practice_elapsed_ms = 0       -- elapsed at clear/abort (for display + result)
local practice_completed  = false   -- true if clear, false if abort
local rating_input_last   = {}      -- for debouncing L+D-pad
```

- [ ] **Step 4: Add the JSON field extractor helper to HELPERS section**

```lua
-- Extract a string field from a flat JSON object string.
-- Handles backslash-escaped backslashes (e.g. Windows paths).
local function json_get_str(json_str, key)
  local raw = json_str:match('"' .. key .. '"%s*:%s*"(.-)"[%s,}%]]')
  if not raw then return nil end
  -- unescape in the correct order: \\ -> \ first, then \" -> "
  return (raw:gsub('\\\\', '\\'):gsub('\\"', '"'))
end

-- Extract a number field from a flat JSON object string.
local function json_get_num(json_str, key)
  return tonumber(json_str:match('"' .. key .. '"%s*:%s*(%d+)'))
end

-- Parse practice_load JSON payload into a table.
local function parse_practice_split(json_str)
  return {
    id                = json_get_str(json_str, "id") or "",
    state_path        = json_get_str(json_str, "state_path") or "",
    goal              = json_get_str(json_str, "goal") or "",
    description       = json_get_str(json_str, "description") or "",
    reference_time_ms = json_get_num(json_str, "reference_time_ms"),
  }
end
```

- [ ] **Step 5: Add new TCP command handlers in `handle_tcp()`**

In the `handle_tcp()` command dispatch (after the existing `elseif` branches, before the
`else` for unknown commands):

```lua
      elseif line:sub(1, 14) == "practice_load:" then
        local json_str = line:sub(15)
        practice_split    = parse_practice_split(json_str)
        practice_mode     = true
        practice_state    = PSTATE_LOADING
        pending_load      = practice_split.state_path
        practice_start_ms = ts_ms()
        client:send("ok:queued\n")
        log("Practice load queued: " .. (practice_split.id or "?"))

      elseif line == "practice_stop" then
        practice_mode     = false
        practice_state    = PSTATE_IDLE
        practice_split    = nil
        pending_load      = nil  -- prevent ghost reload if stopped mid-death-retry
        rating_input_last = {}   -- clear debounce state
        client:send("ok\n")
        log("Practice mode stopped")
```

- [ ] **Step 6: Reload script in Mesen2, send `practice_load` and `practice_stop` via netcat/Python, verify log output**

From a terminal (or Python REPL):
```python
import socket, json
s = socket.socket(); s.connect(("127.0.0.1", 15482))
payload = {"id":"test:1:0:normal","state_path":"C:/fake/path.mss","goal":"normal","description":"","reference_time_ms":10000}
s.sendall(("practice_load:" + json.dumps(payload) + "\n").encode())
print(s.recv(64))  # expect b'ok:queued\n'
s.sendall(b"practice_stop\n")
print(s.recv(64))  # expect b'ok\n'
s.close()
```

Check Mesen2 log: should see "Practice load queued: test:1:0:normal" and "Practice mode stopped".

- [ ] **Step 7: Commit**

```bash
git add lua/spinlab.lua
git commit -m "feat(lua): add practice mode state vars and TCP command handlers"
```

---

### Task 5: Practice mode state machine

**Files:**
- Modify: `lua/spinlab.lua`

- [ ] **Step 1: Add `check_rating_input()` helper**

Add to HELPERS section:

```lua
-- Returns rating string if L+D-pad combo detected (debounced), else nil.
-- L+Left=again, L+Down=hard, L+Right=good, L+Up=easy
local function check_rating_input()
  local inp = emu.getInput(0)
  if not inp or not inp.l then
    rating_input_last = {}
    return nil
  end
  -- Debounce: only fire on the first frame a combo is detected
  local combo = (inp.left and "again")
             or (inp.down  and "hard")
             or (inp.right and "good")
             or (inp.up    and "easy")
  if combo and not rating_input_last[combo] then
    rating_input_last = { [combo] = true }
    return combo
  end
  if not combo then rating_input_last = {} end
  return nil
end
```

- [ ] **Step 2: Add `handle_practice(curr)` function**

Add new function (before `on_start_frame`):

```lua
local function handle_practice(curr)
  if practice_state == PSTATE_LOADING then
    -- pending_load was queued; by next frame cpuExec will have fired.
    -- Transition to PLAYING and start the timer.
    practice_state    = PSTATE_PLAYING
    practice_start_ms = ts_ms()

  elseif practice_state == PSTATE_PLAYING then
    -- Death check first (higher priority than exit_mode)
    if curr.player_anim == 9 and prev.player_anim ~= 9 then
      pending_load      = practice_split.state_path
      practice_start_ms = ts_ms()
      log("Practice: death — reloading state")
      -- stay in PSTATE_PLAYING

    elseif curr.exit_mode ~= 0 and prev.exit_mode == 0 then
      local goal = goal_type(curr)
      practice_elapsed_ms = ts_ms() - practice_start_ms
      practice_completed  = (goal ~= "abort")
      practice_state      = PSTATE_RATING
      log("Practice: " .. (practice_completed and "clear" or "abort")
          .. " goal=" .. goal .. " elapsed=" .. practice_elapsed_ms .. "ms")
    end

  elseif practice_state == PSTATE_RATING then
    local rating = check_rating_input()
    if rating then
      -- Send result to Python
      local result = {
        event      = "attempt_result",
        split_id   = practice_split.id,
        completed  = practice_completed,
        time_ms    = practice_elapsed_ms,
        goal       = practice_split.goal,
        rating     = rating,
      }
      if client then
        client:send(to_json(result) .. "\n")
        log("Practice: sent attempt_result rating=" .. rating)
      end
      -- Reset
      practice_mode  = false
      practice_state = PSTATE_IDLE
      practice_split = nil
    end
  end
end
```

- [ ] **Step 3: Modify `on_start_frame` to branch on practice_mode**

In `on_start_frame`, replace:
```lua
  local curr = read_mem()
  detect_transitions(curr)
  prev = curr
```

With:
```lua
  local curr = read_mem()
  if practice_mode then
    handle_practice(curr)
  else
    detect_transitions(curr)
  end
  prev = curr
```

- [ ] **Step 4: Manually test the practice loop (partial)**

Open Mesen2 with the script running. From a Python REPL, send a `practice_load` with
a known valid state path. Verify:
- State loads (emulator jumps to the saved level start)
- Die and confirm the state reloads automatically
- Complete the level; check Mesen2 log shows "clear" and awaits rating
- Press L+Right on the controller; verify log shows "sent attempt_result rating=good"
- Verify Python REPL (if listening) receives the JSON line

- [ ] **Step 5: Commit**

```bash
git add lua/spinlab.lua
git commit -m "feat(lua): add practice mode state machine and rating input"
```

---

### Task 6: Practice overlay

**Files:**
- Modify: `lua/spinlab.lua`

- [ ] **Step 1: Verify `drawString` rendering before writing overlay code**

In `on_start_frame`, temporarily replace the existing overlay:
```lua
  emu.drawString(2, 2, "SpinLab", 0xFFFFFF, 0x000000, 1)
```
with a longer test string:
```lua
  emu.drawString(2, 2, "SpinLab Test 0123", 0xFFFFFF, 0x000000, 1)
```

Load the script in Mesen2. **Does the string render horizontally in one line?**

- If YES: rendering works, proceed to Step 2.
- If NO (vertical / stacked chars): the implementation must use multiple short
  `drawString` calls for each word, spacing them manually by measured character width.
  Run `emu.measureString("A", 1)` in a probe to get char dimensions, then calculate
  x-offsets. Ask for guidance before implementing this workaround.

Revert the test string before proceeding.

- [ ] **Step 2: Add `draw_practice_overlay()` function**

Add to the HELPERS section:

```lua
local function ms_to_display(ms)
  -- Format milliseconds as M:SS.d (e.g. 75340 -> "1:15.3")
  if not ms then return "?" end
  local total_s = math.floor(ms / 100) / 10
  local m = math.floor(total_s / 60)
  local s = total_s - m * 60
  return string.format("%d:%04.1f", m, s)
end

local function draw_practice_overlay()
  if not practice_mode then return end

  if practice_state == PSTATE_PLAYING or practice_state == PSTATE_LOADING then
    local elapsed = ts_ms() - practice_start_ms
    local ref = practice_split.reference_time_ms
    local ref_str = ref and ms_to_display(ref) or "?"
    emu.drawString(2, 2,
      "[PRACTICE] " .. (practice_split.goal or "?")
      .. " " .. ms_to_display(elapsed)
      .. " ref:" .. ref_str,
      0xFFFFFF, 0x000000, 1)

  elseif practice_state == PSTATE_RATING then
    local prefix = practice_completed and "Clear!" or "Abort"
    emu.drawString(2, 2,
      prefix .. " " .. ms_to_display(practice_elapsed_ms),
      0xFFFFFF, 0x000000, 1)
    emu.drawString(2, 12,
      "L+< again  L+v hard  L+> good  L+^ easy",
      0xFFFFFF, 0x000000, 1)
  end
end
```

- [ ] **Step 3: Call `draw_practice_overlay()` in `on_start_frame`**

Replace the existing always-on overlay line:
```lua
  emu.drawString(2, 2, "SpinLab", 0xFFFFFF, 0x000000, 1)
```
With:
```lua
  if not practice_mode then
    emu.drawString(2, 2, "SpinLab", 0xFFFFFF, 0x000000, 1)
  end
  draw_practice_overlay()
```

- [ ] **Step 4: Test overlay in Mesen2**

Load the script. Send a `practice_load`. Verify:
- PLAYING state: overlay shows split info and a running timer
- RATING state: overlay shows "Clear!" or "Abort" with time and the rating prompt
- After rating: overlay disappears (back to "SpinLab")

- [ ] **Step 5: Commit**

```bash
git add lua/spinlab.lua
git commit -m "feat(lua): add practice overlay"
```

---

### Task 7: End-to-end integration test

- [ ] **Step 1: Start Mesen2 with the script**

Run `scripts/launch.bat` (or manually open Mesen2, load ROM, load Lua script).
Confirm log shows "SpinLab initialized".

- [ ] **Step 2: Run the orchestrator**

From the repo root (with venv active):
```
python -m spinlab.orchestrator
```

Expected output:
```
Connecting to Lua on 127.0.0.1:15482 (waiting up to 30s)...
Connected.
```

- [ ] **Step 3: Verify the first split loads**

The orchestrator picks the first due split and sends `practice_load`. Mesen2 should
jump to that level's start (level card animation plays). The overlay should show
`[PRACTICE] normal 0:00.0 ref:X.X`.

- [ ] **Step 4: Play through a split**

Complete the level normally. Verify:
- RATING overlay appears: "Clear! X.X ref:X.X" + rating prompt
- Press L+Right (good). Overlay disappears.
- Orchestrator terminal prints: `✓ smw_cod:X:X:normal  good  NNNms`
- Next split loads immediately (no perceptible lag)

- [ ] **Step 5: Verify auto-retry on death**

During a split attempt, intentionally die. Verify the same state reloads automatically
with no rating prompt.

- [ ] **Step 6: Verify abort**

Press Start+Select during a split. Verify:
- RATING overlay shows "Abort X.X"
- Rate with L+Left (again)
- Orchestrator prints: `✗ smw_cod:X:X:normal  again  NNNms`

- [ ] **Step 7: Verify DB contains session data**

```python
import sqlite3
conn = sqlite3.connect("data/spinlab.db")
conn.row_factory = sqlite3.Row
print(list(conn.execute("SELECT * FROM attempts ORDER BY created_at DESC LIMIT 5").fetchall()))
print(list(conn.execute("SELECT * FROM sessions ORDER BY started_at DESC LIMIT 1").fetchall()))
```

Confirm attempts and session rows exist with correct data.

- [ ] **Step 8: Stop with Ctrl+C, verify clean shutdown**

```
^C
Stopping...
Session ended: N attempts, N completed.
```

Verify no error traceback.

- [ ] **Step 9: Final commit**

```bash
git add -A
git commit -m "feat: Step 4 complete — practice loop MVP"
```

---

## Notes for Implementer

**`emu.getInput(0)` field names:** The probe step in Task 4 will reveal the actual
field names. Expected: `l`, `r`, `up`, `down`, `left`, `right`, `a`, `b`, `x`, `y`,
`start`, `select`. Update `check_rating_input()` if names differ.

**`emu.drawString` vertical rendering bug:** The overlay verification step in Task 6
must be done before writing final overlay code. Prior sessions have seen characters
rendered vertically for longer strings. If this occurs, seek guidance before proceeding.

**Windows paths in JSON:** The `parse_practice_split` helper uses Lua pattern matching
to extract JSON fields. It handles `\\`-escaped backslashes (Windows paths). Verify
with a real state path during Task 4 Step 6.

**SM-2 `pick_next()` behavior:** On the very first session (no prior attempts), all
splits have `next_review = now` (set by `ensure_schedule`). The scheduler will return
splits ordered oldest-first. This is correct — round-robin for the first pass.

**`goal_matched` is always `True` for completed attempts in MVP:** Lua only
differentiates "abort" vs non-abort. The `goal_matched` field in the attempt will be
`True` for all clears and `None` for aborts. This is correct behavior for MVP.

**TCP disconnect during a session:** `ConnectionError` propagates out of the main loop,
hits the `finally` block (which closes the socket + ends the session cleanly), then
exits. Single-reconnect retry is deferred to a future step.
