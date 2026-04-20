# Codebase Hygiene Sweep — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix a protocol bug, remove dead code, clean up public/private API boundaries, extract Lua magic numbers, deduplicate test helpers, and mark slow tests.

**Architecture:** Ten focused tasks spanning Lua, Python, and tests. Tasks 1-3 are Lua-only (protocol fix, dead code removal, constants). Task 4 is the Python API cleanup (SessionManager public surface + StateBuilder + routes). Tasks 5-10 are smaller Python/test cleanups. Each task is independently committable.

**Tech Stack:** Python 3.11+, Lua (Mesen2), pytest, FastAPI

---

### Task 1: Fix `fill_gap_load` Lua handler + harden unknown-command error path

The Python `FillGapLoadCmd` sends `{"event": "fill_gap_load", ...}` but Lua only handles `cold_fill_load`. The `fill_gap_load` message falls through to `err:unknown_command`. Also, unknown JSON commands only send a terse error back — add a `log()` call so it shows up in Mesen's script log.

**Files:**
- Modify: `lua/spinlab.lua:1240-1254` (add fill_gap_load handler next to cold_fill_load)
- Modify: `lua/spinlab.lua:1325-1331` (the else branch at end of handle_json_message — add log)
- Test: Manual — start dashboard, trigger fill-gap from segments tab, confirm emulator loads state

- [ ] **Step 1: Add `fill_gap_load` handler in `handle_json_message`**

Insert a new `elseif` branch right before the existing `cold_fill_load` handler. `fill_gap_load` is simpler than `cold_fill_load` — it doesn't track a segment_id for cold-fill state machine, it just loads the state and enters cold_fill mode so the player can die and capture the cold start.

In `lua/spinlab.lua`, right before line 1240 (`elseif decoded_event == "cold_fill_load" then`), add:

```lua
  elseif decoded_event == "fill_gap_load" then
    local path = json_get_str(line, "state_path")
    if not path then
      client:send(to_json({event = "error", message = "fill_gap_load requires state_path"}) .. "\n")
    else
      table.insert(pending_loads, path)
      cold_fill.active = true
      cold_fill.state = CFSTATE_WAITING_DEATH
      cold_fill.segment_id = "fill_gap"
      cold_fill.prev_anim = 0
      cold_fill.prev_level_start = 0
      client:send("ok:fill_gap\n")
      log("Fill-gap: loaded state -- die to capture cold start")
    end
```

- [ ] **Step 2: Harden the unknown-command else branch**

In `lua/spinlab.lua`, at the bottom of `handle_json_message` (around line 1325-1331), the current else branch is:

```lua
  else
    client:send(to_json({event = "error", message = "unknown command: " .. tostring(decoded_event)}) .. "\n")
  end
```

If it doesn't already have a `log()` call, add one:

```lua
  else
    log("ERROR: unknown JSON command: " .. tostring(decoded_event))
    client:send(to_json({event = "error", message = "unknown command: " .. tostring(decoded_event)}) .. "\n")
  end
```

Also do the same for the text dispatch fallthrough at line 1455:

```lua
  log("ERROR: unknown command: " .. line)
  client:send("err:unknown_command\n")
```

- [ ] **Step 3: Commit**

```bash
git add lua/spinlab.lua
git commit -m "fix: add fill_gap_load Lua handler; log unknown commands"
```

---

### Task 2: Drop unused SystemState sub-state dataclasses

`system_state.py` defines `CaptureState`, `DraftState`, `ColdFillState`, `FillGapState`, `PracticeState` — none are ever instantiated. SessionManager only uses `SystemState.mode`, `.game_id`, `.game_name`. Remove the dead sub-states and their unused fields from `SystemState`.

**Files:**
- Modify: `python/spinlab/system_state.py` (remove 5 dataclasses + their fields from SystemState)
- Test: `pytest tests/unit/test_session_manager.py -v` (verify nothing breaks)

- [ ] **Step 1: Strip `system_state.py` down to what's actually used**

Replace the entire file content with:

```python
"""SystemState — single source of truth for what the system is doing right now."""
from __future__ import annotations

from dataclasses import dataclass

from .models import Mode


@dataclass
class SystemState:
    """Single source of truth for the system's current mode and associated sub-state."""
    mode: Mode = Mode.IDLE
    game_id: str | None = None
    game_name: str | None = None
```

- [ ] **Step 2: Run tests**

Run: `pytest tests/unit/ -x -q`
Expected: All pass — nothing imports the removed dataclasses.

- [ ] **Step 3: Commit**

```bash
git add python/spinlab/system_state.py
git commit -m "refactor: remove unused SystemState sub-state dataclasses"
```

---

### Task 3: Remove deprecated Lua text/prefixed command handlers

The `text_commands` and `prefixed_commands` tables (lines 1335-1422) are marked deprecated and duplicated by JSON handlers. Python only uses `send_command()` which sends JSON. Remove both tables and their dispatch branches in `tcp_dispatch()`.

**Files:**
- Modify: `lua/spinlab.lua:1333-1422` (delete text_commands and prefixed_commands tables)
- Modify: `lua/spinlab.lua:1438-1453` (delete the dispatch branches that call them)
- Test: `pytest -m emulator -v` (verify the emulator integration tests still work — they use JSON commands)

- [ ] **Step 1: Delete `text_commands` table**

Remove lines 1335-1369 (the `local text_commands = { ... }` block, including the deprecation comments above it at 1333-1334).

- [ ] **Step 2: Delete `prefixed_commands` table**

Remove lines 1371-1422 (the `local prefixed_commands = { ... }` block).

- [ ] **Step 3: Simplify `tcp_dispatch`**

The current `tcp_dispatch` (starting around line 1424) has this flow:
1. Check poke_handler
2. If starts with `{` → `handle_json_message(line)`
3. Check text_commands exact match
4. Check prefixed_commands prefix match
5. `err:unknown_command`

Remove steps 3 and 4 so it becomes:

```lua
local function tcp_dispatch(line)
  log("TCP received: " .. line)

  -- Extension hook: let external scripts handle messages first
  if poke_handler then
    local handled = poke_handler(line)
    if handled then return end
  end

  if line:sub(1, 1) == "{" then
    handle_json_message(line)
    return
  end

  log("ERROR: unknown command: " .. line)
  client:send("err:unknown_command\n")
end
```

- [ ] **Step 4: Commit**

```bash
git add lua/spinlab.lua
git commit -m "refactor: remove deprecated text/prefixed Lua command handlers"
```

---

### Task 4: Extract Lua I/O port magic numbers to named constants

SPC I/O port values `3`, `4`, `7`, `8` appear in `goal_type()` and `check_checkpoint_hit()` without names. Add constants to `addresses.lua` (the single source of truth for memory/hardware values).

**Files:**
- Modify: `lua/addresses.lua` (add 4 constants)
- Modify: `lua/spinlab.lua:614-621,682-685` (use the named constants)

- [ ] **Step 1: Add constants to `addresses.lua`**

Append to the end of `lua/addresses.lua`:

```lua

-- SPC I/O port values (read from ADDR_IO / 0x1DFB)
IO_ORB     = 3   -- collected orb/dragon coin
IO_GOAL    = 4   -- normal goal tape/gate
IO_KEY     = 7   -- collected secret exit key
IO_FADEOUT = 8   -- screen fadeout (pipe/door exit)
```

- [ ] **Step 2: Replace magic numbers in `goal_type()`**

Change `lua/spinlab.lua` lines 614-621 from:

```lua
local function goal_type(curr)
  if curr.io_port == 7 then return "key"
  elseif curr.io_port == 3 then return "orb"
  elseif curr.boss_defeat ~= 0 and curr.fanfare == 1 then return "boss"
  elseif curr.fanfare == 1 or curr.io_port == 4 then return "normal"
  else return "abort"  -- start+select, death exit, etc.
  end
end
```

To:

```lua
local function goal_type(curr)
  if curr.io_port == IO_KEY then return "key"
  elseif curr.io_port == IO_ORB then return "orb"
  elseif curr.boss_defeat ~= 0 and curr.fanfare == 1 then return "boss"
  elseif curr.fanfare == 1 or curr.io_port == IO_GOAL then return "normal"
  else return "abort"  -- start+select, death exit, etc.
  end
end
```

- [ ] **Step 3: Replace magic numbers in `check_checkpoint_hit()`**

Change `lua/spinlab.lua` lines 682-685 from:

```lua
  local got_orb     = curr.io_port == 3
  local got_goal    = curr.fanfare == 1 or curr.io_port == 4
  local got_key     = curr.io_port == 7
  local got_fadeout = curr.io_port == 8
```

To:

```lua
  local got_orb     = curr.io_port == IO_ORB
  local got_goal    = curr.fanfare == 1 or curr.io_port == IO_GOAL
  local got_key     = curr.io_port == IO_KEY
  local got_fadeout = curr.io_port == IO_FADEOUT
```

- [ ] **Step 4: Commit**

```bash
git add lua/addresses.lua lua/spinlab.lua
git commit -m "refactor: extract Lua I/O port magic numbers to named constants"
```

---

### Task 5: DRY Lua practice/speed-run reset

`practice_reset()` and `speed_run_reset()` share the same structural pattern. Extract the shared fields into a helper.

**Files:**
- Modify: `lua/spinlab.lua:147-189`

- [ ] **Step 1: Add a shared reset helper**

Add above `practice_reset()` (before line 147):

```lua
local function reset_mode_state(tbl)
    tbl.active = false
    tbl.state = PSTATE_IDLE
    tbl.segment = nil
    tbl.start_ms = 0
    tbl.elapsed_ms = 0
    tbl.result_start_ms = 0
    tbl.auto_advance_ms = AUTO_ADVANCE_DEFAULT_MS
    reset_transition_state()
end
```

- [ ] **Step 2: Rewrite `practice_reset()` to use the helper**

```lua
local function practice_reset()
    reset_mode_state(practice)
    practice.completed = false
    practice.deaths = 0
    practice.last_death_ms = 0
end
```

- [ ] **Step 3: Rewrite `speed_run_reset()` to use the helper**

```lua
local function speed_run_reset()
    reset_mode_state(speed_run)
    speed_run.split_ms = 0
    speed_run.respawn_path = ""
    speed_run.cp_index = 0
    speed_run.result_split_ms = 0
end
```

- [ ] **Step 4: Commit**

```bash
git add lua/spinlab.lua
git commit -m "refactor: DRY practice/speed-run reset into shared helper"
```

---

### Task 6: SessionManager public API cleanup

Routes and StateBuilder access private methods `_get_scheduler()`, `_require_game()`, `_replay_frame`, `_replay_total`. Make these public. Also make `_install_condition_registry` public since tests call it.

**Files:**
- Modify: `python/spinlab/session_manager.py` (rename private → public)
- Modify: `python/spinlab/state_builder.py` (update references)
- Modify: `python/spinlab/routes/model.py` (update references)
- Modify: `python/spinlab/routes/reference.py` (update reference)
- Modify: `tests/unit/test_state_builder.py` (update references)
- Modify: `tests/unit/test_invalidate_flow.py` (update references)
- Modify: `tests/unit/test_session_manager.py` (update references if any)

- [ ] **Step 1: Rename privates to public in `session_manager.py`**

In `python/spinlab/session_manager.py`, make these renames (use find-and-replace within the file):

| Old | New |
|-----|-----|
| `_get_scheduler` | `get_scheduler` |
| `_require_game` | `require_game` |
| `_replay_frame` | `replay_frame` |
| `_replay_total` | `replay_total` |
| `_install_condition_registry` | `install_condition_registry` |

These are all referenced externally and should have been public from the start. The remaining private methods (`_clear_ref_and_idle`, `_apply_result`, `_handle_*`, `_on_practice_done`, `_on_speed_run_done`, `_notify_sse`) are genuinely internal and stay private.

- [ ] **Step 2: Update `state_builder.py`**

In `python/spinlab/state_builder.py`, change:
- Line 47: `session._get_scheduler()` → `session.get_scheduler()`
- Line 62: `session._replay_frame` → `session.replay_frame`
- Line 63: `session._replay_total` → `session.replay_total`

- [ ] **Step 3: Update `routes/model.py`**

In `python/spinlab/routes/model.py`, change all 5 occurrences:
- Line 25: `session._get_scheduler()` → `session.get_scheduler()`
- Line 60: `session._get_scheduler()` → `session.get_scheduler()`
- Line 77: `session._get_scheduler()` → `session.get_scheduler()`
- Line 86: `session._get_scheduler()` → `session.get_scheduler()`
- Line 105: `session._get_scheduler()` → `session.get_scheduler()`

- [ ] **Step 4: Update `routes/reference.py`**

In `python/spinlab/routes/reference.py`, change:
- Line 61: `session._require_game()` → `session.require_game()`

- [ ] **Step 5: Update test files**

In `tests/unit/test_state_builder.py`, change all 4 occurrences:
- Lines 48, 92, 119, 140: `sm._get_scheduler` → `sm.get_scheduler`

In `tests/unit/test_invalidate_flow.py`, change all 3 occurrences:
- Lines 73, 92, 108: `sm._install_condition_registry` → `sm.install_condition_registry`

- [ ] **Step 6: Run tests**

Run: `pytest tests/unit/ -x -q`
Expected: All pass.

- [ ] **Step 7: Commit**

```bash
git add python/spinlab/session_manager.py python/spinlab/state_builder.py python/spinlab/routes/model.py python/spinlab/routes/reference.py tests/unit/test_state_builder.py tests/unit/test_invalidate_flow.py
git commit -m "refactor: make SessionManager scheduler/game/replay API public"
```

---

### Task 7: Deduplicate test DB fixture and helpers

The `db(tmp_path)` fixture and `_make_seg_with_state()` helper are copy-pasted across 4 test files. Move them to `tests/conftest.py` (the shared fixture already exists there for `mock_db`).

**Files:**
- Modify: `tests/conftest.py` (add `practice_db` fixture and `make_seg_with_state` helper)
- Modify: `tests/unit/test_practice.py` (remove local copies, use shared fixture)
- Modify: `tests/unit/test_practice_coverage.py` (remove local copies, use shared fixture)

Note: `test_speed_run_mode.py` and `test_cold_fill_integration.py` have different DB setup patterns (multi-level games, different game IDs) so they keep their own fixtures — forcing them into a shared fixture would over-abstract.

- [ ] **Step 1: Add shared helper and fixture to `tests/conftest.py`**

Add at the bottom of `tests/conftest.py`:

```python
from spinlab.db import Database
from spinlab.models import Segment, Waypoint, WaypointSaveState


def make_seg_with_state(db, game_id, level, start_type, end_type,
                        state_path, ordinal=1):
    """Create waypoints + segment + hot save state; return segment."""
    wp_start = Waypoint.make(game_id, level, start_type, 0, {})
    wp_end = Waypoint.make(game_id, level, end_type, 0, {})
    db.upsert_waypoint(wp_start)
    db.upsert_waypoint(wp_end)
    seg = Segment(
        id=Segment.make_id(game_id, level, start_type, 0, end_type, 0,
                           wp_start.id, wp_end.id),
        game_id=game_id, level_number=level,
        start_type=start_type, start_ordinal=0,
        end_type=end_type, end_ordinal=0,
        description=f"L{level}" if start_type == "entrance" else "",
        ordinal=ordinal,
        start_waypoint_id=wp_start.id, end_waypoint_id=wp_end.id,
    )
    db.upsert_segment(seg)
    db.add_save_state(WaypointSaveState(
        waypoint_id=wp_start.id, variant_type="hot",
        state_path=str(state_path), is_default=True,
    ))
    return seg


@pytest.fixture
def practice_db(tmp_path):
    """Real DB with one game + one entrance→goal segment for practice tests."""
    d = Database(tmp_path / "test.db")
    d.upsert_game("g", "Game", "any%")
    state_file = tmp_path / "test.mss"
    state_file.write_bytes(b"fake state")
    seg = make_seg_with_state(d, "g", 1, "entrance", "goal", state_file)
    d._test_seg_id = seg.id
    d._test_state_file = state_file
    return d
```

- [ ] **Step 2: Update `tests/unit/test_practice.py`**

Remove the local `_make_seg_with_state` function (lines 14-36) and the local `db` fixture (lines 39-48). Replace:
- All calls to `_make_seg_with_state(` → `make_seg_with_state(` (import from conftest)
- Replace `db` fixture references with `practice_db` in test function signatures

Add at the top of the file:

```python
from tests.conftest import make_seg_with_state
```

And rename the `db` parameter in each test function to `practice_db`.

- [ ] **Step 3: Update `tests/unit/test_practice_coverage.py`**

Same changes as step 2 — remove local `_make_seg_with_state` (lines 14-36) and local `db` fixture (lines 39-47). Import shared helper, use `practice_db` fixture.

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/test_practice.py tests/unit/test_practice_coverage.py -v`
Expected: All pass.

- [ ] **Step 5: Commit**

```bash
git add tests/conftest.py tests/unit/test_practice.py tests/unit/test_practice_coverage.py
git commit -m "refactor: deduplicate practice test DB fixture and seg helper"
```

---

### Task 8: Fix tests that access private recorder internals

`test_recorder.py` asserts on `cap._deaths_in_segment` and `cap._last_spawn_ms`. These should test observable behavior (the `segment_times` output) instead.

**Files:**
- Modify: `tests/unit/capture/test_recorder.py:75-107`

- [ ] **Step 1: Fix `test_clear_resets_segment_times`**

The test at line 75 currently asserts:
```python
assert cap._deaths_in_segment == 0
assert cap._last_spawn_ms is None
```

Replace those two lines with a behavioral assertion — after `clear()`, recording a new segment should start fresh with 0 deaths:

```python
    # After clear, a new segment should start fresh
    cap.handle_entrance({"level": 2, "timestamp_ms": 10000, "state_path": "/s2.mss"})
    cap.handle_exit({"level": 2, "goal": "goal", "timestamp_ms": 15000}, "g1", db, registry)
    assert cap.segment_times[0].deaths == 0
    assert cap.segment_times[0].clean_tail_ms == 5000
```

- [ ] **Step 2: Fix `test_death_via_handle_death_increments_counter`**

The test at line 97 asserts `cap._deaths_in_segment == 2`. Instead, complete the segment and check the output:

```python
def test_death_via_handle_death_increments_counter(db, registry):
    """Two deaths during a segment are reflected in the recorded segment time."""
    cap = SegmentRecorder()
    cap.capture_run_id = "run1"
    cap.handle_entrance({
        "level": 1, "state_path": "/s.mss",
        "conditions": {}, "timestamp_ms": 1000,
    })
    cap.handle_death(timestamp_ms=2000)
    cap.handle_death(timestamp_ms=3000)
    cap.handle_spawn_timing(timestamp_ms=4000)
    cap.handle_exit({"level": 1, "goal": "goal", "timestamp_ms": 6000}, "g1", db, registry)

    assert cap.segment_times[0].deaths == 2
```

- [ ] **Step 3: Run tests**

Run: `pytest tests/unit/capture/test_recorder.py -v`
Expected: All pass.

- [ ] **Step 4: Commit**

```bash
git add tests/unit/capture/test_recorder.py
git commit -m "refactor: test recorder behavior via segment_times, not private fields"
```

---

### Task 9: Add missing `@pytest.mark.slow` markers

Two test files use `asyncio.sleep()` but aren't marked slow:
- `test_session_manager.py:442` — `asyncio.sleep(10)` (monkeypatched to 0.1s)
- `test_speed_run_mode.py` — multiple `0.02-0.05s` sleeps

**Files:**
- Modify: `tests/unit/test_session_manager.py:442`
- Modify: `tests/unit/test_speed_run_mode.py` (class-level or function-level marks)

- [ ] **Step 1: Mark the hung-task test in `test_session_manager.py`**

Add `@pytest.mark.slow` to `test_stop_practice_cancels_hung_task` (line 442):

```python
    @pytest.mark.slow
    async def test_stop_practice_cancels_hung_task(self, mock_db, mock_tcp, monkeypatch):
```

- [ ] **Step 2: Identify and mark slow tests in `test_speed_run_mode.py`**

The tests with `asyncio.sleep()` are in the functions that call `sr.run_one()` or `sr.run_loop()`. Find the test class(es) or individual functions that use sleeps and add `@pytest.mark.slow`.

Look at the test functions that use `asyncio.sleep`. These are:
- Tests calling `run_one()` and `run_loop()` with async delivery helpers

Add `@pytest.mark.slow` to each such test function (or to the class if every method in the class uses sleeps).

- [ ] **Step 3: Verify fast tests skip them**

Run: `pytest -m "not (emulator or slow or frontend)" --co -q | grep -c "test_"`
Expected: The count should be lower than before (the newly-marked tests should be excluded).

- [ ] **Step 4: Commit**

```bash
git add tests/unit/test_session_manager.py tests/unit/test_speed_run_mode.py
git commit -m "fix: add missing @pytest.mark.slow to tests with asyncio.sleep"
```

---

### Task 10: Consolidate `RECENT_ATTEMPTS_LIMIT`

The same `8` is defined as `RECENT_ATTEMPTS_LIMIT` in `state_builder.py:20` and `RECENT_ATTEMPTS_DB_LIMIT` in `db/attempts.py:39`. They're the same value used for the same purpose. Keep the one in `db/attempts.py` (it's closer to the query that uses it) and import it in `state_builder.py`.

**Files:**
- Modify: `python/spinlab/state_builder.py:20` (remove constant, import from db)
- Test: `pytest tests/unit/test_state_builder.py tests/unit/test_dashboard_integration.py -v`

- [ ] **Step 1: Remove the constant from `state_builder.py` and import from db**

In `python/spinlab/state_builder.py`:

Remove line 20:
```python
RECENT_ATTEMPTS_LIMIT = 8
```

Add the import — update the TYPE_CHECKING block to a real import (since we need the value at runtime):

Add to the imports section:
```python
from .db.attempts import RECENT_ATTEMPTS_DB_LIMIT
```

Then change line 76 from:
```python
            session.game_id, limit=RECENT_ATTEMPTS_LIMIT,
```
To:
```python
            session.game_id, limit=RECENT_ATTEMPTS_DB_LIMIT,
```

- [ ] **Step 2: Run tests**

Run: `pytest tests/unit/test_state_builder.py tests/unit/test_dashboard_integration.py -v`
Expected: All pass.

- [ ] **Step 3: Commit**

```bash
git add python/spinlab/state_builder.py
git commit -m "refactor: consolidate RECENT_ATTEMPTS_LIMIT to single definition in db.attempts"
```

---

## Post-implementation: run full test suite

After all tasks are done:

```bash
pytest
```

All tests must pass. Fix any failures before committing.
