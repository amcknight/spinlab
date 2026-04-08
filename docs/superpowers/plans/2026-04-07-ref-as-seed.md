# Reference-as-Seed Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make practice Total time account for death overhead, then seed estimators with reference run segment times so the model has data before the first practice session.

**Architecture:** Three layers: (1) Lua practice mode tracks deaths and adds a configurable penalty to elapsed time, also tracks clean_tail_ms; (2) Python `ReferenceCapture` accumulates per-segment timing during capture; (3) `DraftManager.save()` inserts seed attempts and triggers estimator rebuild. A new `AttemptSource.REFERENCE` distinguishes seeds from practice data.

**Tech Stack:** Python 3.11+, Lua (Mesen2), SQLite, pytest, YAML config

---

### Task 1: Add `death_penalty_ms` to per-game config

Load `death_penalty_ms` from the per-game `conditions.yaml` and expose it on `ConditionRegistry`.

**Files:**
- Modify: `python/spinlab/condition_registry.py:46-70` (ConditionRegistry class + from_yaml)
- Modify: `python/spinlab/games/abcdef0123456789/conditions.yaml` (example config)
- Test: `tests/test_condition_registry.py` (new or existing)

- [ ] **Step 1: Write failing test for death_penalty_ms loading**

```python
# tests/test_condition_registry.py
from pathlib import Path
from spinlab.condition_registry import ConditionRegistry

def test_death_penalty_ms_from_yaml(tmp_path):
    """death_penalty_ms is read from conditions.yaml."""
    yaml_path = tmp_path / "conditions.yaml"
    yaml_path.write_text("death_penalty_ms: 1500\nconditions: []\n")
    reg = ConditionRegistry.from_yaml(yaml_path)
    assert reg.death_penalty_ms == 1500

def test_death_penalty_ms_default(tmp_path):
    """Missing death_penalty_ms defaults to DEFAULT_DEATH_PENALTY_MS."""
    yaml_path = tmp_path / "conditions.yaml"
    yaml_path.write_text("conditions: []\n")
    reg = ConditionRegistry.from_yaml(yaml_path)
    assert reg.death_penalty_ms == 3200

def test_death_penalty_ms_empty_registry():
    """Empty ConditionRegistry() uses the default."""
    reg = ConditionRegistry()
    assert reg.death_penalty_ms == 3200
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_condition_registry.py -v -k "death_penalty"`
Expected: FAIL — `ConditionRegistry` has no `death_penalty_ms` attribute

- [ ] **Step 3: Implement death_penalty_ms on ConditionRegistry**

In `python/spinlab/condition_registry.py`, add a file-level constant and update the dataclass + `from_yaml`:

```python
# At top of file, after imports:
# Time added to practice timer per death to account for death animation +
# respawn that would occur in a real run. Default is standard SMW retry.
DEFAULT_DEATH_PENALTY_MS: int = 3200

# On ConditionRegistry dataclass, add field:
@dataclass
class ConditionRegistry:
    definitions: list[ConditionDef] = field(default_factory=list)
    death_penalty_ms: int = DEFAULT_DEATH_PENALTY_MS

# In from_yaml classmethod, read the value:
    @classmethod
    def from_yaml(cls, path: Path) -> "ConditionRegistry":
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        death_penalty_ms = raw.get("death_penalty_ms", DEFAULT_DEATH_PENALTY_MS)
        defs: list[ConditionDef] = []
        for c in raw.get("conditions", []):
            # ... existing condition parsing unchanged ...
        return cls(definitions=defs, death_penalty_ms=death_penalty_ms)
```

- [ ] **Step 4: Update example conditions.yaml**

Add to `python/spinlab/games/abcdef0123456789/conditions.yaml`:

```yaml
# Time (ms) added to practice timer per death. Accounts for death animation +
# respawn that a real run would include. Standard SMW retry ≈ 3200ms.
# Fast-retry romhacks: ~1000ms.
death_penalty_ms: 3200
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_condition_registry.py -v -k "death_penalty"`
Expected: 3 PASS

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/condition_registry.py python/spinlab/games/abcdef0123456789/conditions.yaml tests/test_condition_registry.py
git commit -m "feat: add death_penalty_ms to per-game config"
```

---

### Task 2: Add `death_penalty_ms` to PracticeLoadCmd and wire through PracticeSession

Pass `death_penalty_ms` from the condition registry through to the Lua `practice_load` command.

**Files:**
- Modify: `python/spinlab/protocol.py:159-166` (PracticeLoadCmd)
- Modify: `python/spinlab/models.py:177-188` (SegmentCommand)
- Modify: `python/spinlab/practice.py:117-158` (run_one)
- Modify: `python/spinlab/session_manager.py:369-389` (start_practice)
- Test: `tests/test_practice.py`

- [ ] **Step 1: Write failing test for death_penalty_ms in practice_load**

```python
# In tests/test_practice.py, add:
@pytest.mark.asyncio
async def test_practice_load_sends_death_penalty_ms(db):
    """practice_load command includes death_penalty_ms from the session."""
    mock_tcp = AsyncMock()
    mock_tcp.is_connected = True
    mock_tcp.send_command = AsyncMock()

    ps = PracticeSession(
        tcp=mock_tcp, db=db, game_id="g",
        death_penalty_ms=2500,
    )
    ps.start()

    # Simulate attempt_result arriving after send
    async def fake_send(cmd):
        if hasattr(cmd, 'event') and cmd.event == "practice_load":
            await asyncio.sleep(0.01)
            ps.receive_result({
                "event": "attempt_result",
                "segment_id": db._test_seg_id,
                "completed": True,
                "time_ms": 5000,
            })
    mock_tcp.send_command = AsyncMock(side_effect=fake_send)

    await ps.run_one()

    sent_cmd = mock_tcp.send_command.call_args_list[0][0][0]
    assert sent_cmd.death_penalty_ms == 2500
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_practice.py::test_practice_load_sends_death_penalty_ms -v`
Expected: FAIL — `PracticeSession.__init__() got an unexpected keyword argument 'death_penalty_ms'`

- [ ] **Step 3: Add death_penalty_ms to PracticeLoadCmd**

In `python/spinlab/protocol.py`, add field to `PracticeLoadCmd`:

```python
@dataclass
class PracticeLoadCmd:
    event: str = "practice_load"
    id: str = ""
    state_path: str = ""
    description: str = ""
    end_type: str = ""
    expected_time_ms: int | None = None
    auto_advance_delay_ms: int = 1000
    death_penalty_ms: int = 3200
```

- [ ] **Step 4: Add death_penalty_ms to SegmentCommand**

In `python/spinlab/models.py`, add field to `SegmentCommand`:

```python
@dataclass
class SegmentCommand:
    """Sent from orchestrator to Lua: which segment to load next."""
    id: str
    state_path: str
    description: str
    end_type: str
    expected_time_ms: int | None = None
    auto_advance_delay_ms: int = 1000
    death_penalty_ms: int = 3200
```

- [ ] **Step 5: Wire death_penalty_ms through PracticeSession**

In `python/spinlab/practice.py`:

Add `death_penalty_ms` parameter to `__init__`:

```python
def __init__(
    self,
    tcp: TcpManager,
    db: Database,
    game_id: str,
    auto_advance_delay_ms: int = 1000,
    death_penalty_ms: int = 3200,
    on_attempt: Callable | None = None,
) -> None:
    # ... existing fields ...
    self.death_penalty_ms = death_penalty_ms
```

In `run_one`, pass it to `SegmentCommand` and `PracticeLoadCmd`:

```python
cmd = SegmentCommand(
    id=picked.segment_id,
    state_path=picked.state_path,
    description=label,
    end_type=picked.end_type,
    expected_time_ms=expected_time_ms,
    auto_advance_delay_ms=self.auto_advance_delay_ms,
    death_penalty_ms=self.death_penalty_ms,
)
# ...
await self.tcp.send_command(PracticeLoadCmd(
    id=cmd.id,
    state_path=cmd.state_path,
    description=cmd.description,
    end_type=cmd.end_type,
    expected_time_ms=cmd.expected_time_ms,
    auto_advance_delay_ms=cmd.auto_advance_delay_ms,
    death_penalty_ms=cmd.death_penalty_ms,
))
```

- [ ] **Step 6: Wire death_penalty_ms in SessionManager.start_practice**

In `python/spinlab/session_manager.py`, `start_practice` method — read from the condition registry:

```python
async def start_practice(self) -> ActionResult:
    # ... existing guards ...
    from .practice import PracticeSession
    death_penalty_ms = self.capture.condition_registry.death_penalty_ms
    ps = PracticeSession(
        tcp=self.tcp, db=self.db, game_id=self._require_game(),
        death_penalty_ms=death_penalty_ms,
        on_attempt=lambda _: asyncio.ensure_future(self._notify_sse()),
    )
    # ... rest unchanged ...
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `python -m pytest tests/test_practice.py -v`
Expected: ALL PASS

- [ ] **Step 8: Commit**

```bash
git add python/spinlab/protocol.py python/spinlab/models.py python/spinlab/practice.py python/spinlab/session_manager.py tests/test_practice.py
git commit -m "feat: wire death_penalty_ms through practice pipeline"
```

---

### Task 3: Lua death penalty and death/clean_tail tracking in practice mode

Add death counting, death penalty timer addition, and clean_tail_ms tracking to the Lua practice state machine. Send `deaths` and `clean_tail_ms` in the `attempt_result` event.

**Files:**
- Modify: `lua/spinlab.lua:141-147` (practice_reset)
- Modify: `lua/spinlab.lua:325-341` (parse_practice_segment)
- Modify: `lua/spinlab.lua:824-883` (handle_practice)
- Test: manual via emulator (Lua tests are integration-only)

- [ ] **Step 1: Add death tracking fields to practice_reset**

In `lua/spinlab.lua`, update `practice_reset()` (around line 141):

```lua
local function practice_reset()
    practice.active = false
    practice.state = PSTATE_IDLE
    practice.segment = nil
    practice.start_ms = 0
    practice.elapsed_ms = 0
    practice.completed = false
    practice.deaths = 0
    practice.last_death_ms = 0  -- timestamp of most recent death reload
end
```

- [ ] **Step 2: Parse death_penalty_ms in parse_practice_segment**

In `parse_practice_segment` (around line 326), add to the returned table:

```lua
local function parse_practice_segment(json_str)
  local end_on_goal = json_get_bool(json_str, "end_on_goal")
  if end_on_goal == nil then end_on_goal = true end
  local end_type = json_get_str(json_str, "end_type") or "goal"
  return {
    id                     = json_get_str(json_str, "id") or "",
    state_path             = json_get_str(json_str, "state_path") or "",
    goal                   = json_get_str(json_str, "goal") or "",
    description            = json_get_str(json_str, "description") or "",
    reference_time_ms      = json_get_num(json_str, "reference_time_ms"),
    auto_advance_delay_ms  = json_get_num(json_str, "auto_advance_delay_ms") or AUTO_ADVANCE_DEFAULT_MS,
    expected_time_ms       = json_get_num(json_str, "expected_time_ms"),
    death_penalty_ms       = json_get_num(json_str, "death_penalty_ms") or 3200,
    end_on_goal            = end_on_goal,
    end_type               = end_type,
  }
end
```

- [ ] **Step 3: Update handle_practice death handling**

In `handle_practice` (around line 831), update the death branch in `PSTATE_PLAYING`:

```lua
    -- Death check (higher priority than exit/finish)
    if is_death_frame(curr) then
      practice.deaths = practice.deaths + 1
      practice.elapsed_ms = ts_ms() - practice.start_ms + (practice.segment.death_penalty_ms * practice.deaths)
      practice.last_death_ms = ts_ms()
      table.insert(pending_loads, practice.segment.state_path)
      log("Practice: death #" .. practice.deaths .. " — penalty " .. practice.segment.death_penalty_ms .. "ms, reloading state")
```

Wait — `elapsed_ms` is recomputed on completion as `ts_ms() - practice.start_ms`. The death penalty needs to accumulate separately, otherwise the next frame resets it. Better approach: track cumulative penalty and add it at completion time.

Replace the death and completion logic:

```lua
  elseif practice.state == PSTATE_PLAYING then
    -- Death check (higher priority than exit/finish)
    if is_death_frame(curr) then
      practice.deaths = practice.deaths + 1
      practice.last_death_ms = ts_ms()
      table.insert(pending_loads, practice.segment.state_path)
      log("Practice: death #" .. practice.deaths .. " — reloading state")

    elseif practice.segment.end_type == "checkpoint" and check_checkpoint_hit(curr) then
      local penalty = practice.segment.death_penalty_ms * practice.deaths
      practice.elapsed_ms = ts_ms() - practice.start_ms + penalty
      practice.completed  = true
      practice.state      = PSTATE_RESULT
      practice.result_start_ms = ts_ms()
      log("Practice: CHECKPOINT — " .. practice.elapsed_ms .. "ms (deaths=" .. practice.deaths .. " penalty=" .. penalty .. "ms)")

    elseif practice.segment.end_on_goal and detect_finish(curr) then
      local finish_goal = detect_finish(curr)
      local penalty = practice.segment.death_penalty_ms * practice.deaths
      practice.elapsed_ms = ts_ms() - practice.start_ms + penalty
      practice.completed  = true
      practice.state      = PSTATE_RESULT
      practice.result_start_ms = ts_ms()
      log("Practice: FINISH (" .. finish_goal .. ") — " .. practice.elapsed_ms .. "ms (deaths=" .. practice.deaths .. " penalty=" .. penalty .. "ms)")

    elseif is_exit_frame(curr) then
      local goal = goal_type(curr)
      local penalty = practice.segment.death_penalty_ms * practice.deaths
      practice.elapsed_ms = ts_ms() - practice.start_ms + penalty
      practice.completed  = (goal ~= "abort")
      practice.state      = PSTATE_RESULT
      practice.result_start_ms = ts_ms()
      log("Practice: RESULT (" .. goal .. ") — " .. practice.elapsed_ms .. "ms (deaths=" .. practice.deaths .. " penalty=" .. penalty .. "ms)")
    end
```

- [ ] **Step 4: Send deaths and clean_tail_ms in attempt_result**

In the `PSTATE_RESULT` auto-advance block (around line 863), compute `clean_tail_ms` and include both fields:

```lua
  elseif practice.state == PSTATE_RESULT then
    local elapsed_in_result = ts_ms() - practice.result_start_ms
    if elapsed_in_result >= practice.auto_advance_ms then
      -- Compute clean_tail_ms: time from last death reload to segment end (excluding penalty).
      -- If no deaths, clean_tail equals the raw elapsed time (no penalty).
      local raw_elapsed = practice.elapsed_ms - (practice.segment.death_penalty_ms * practice.deaths)
      local clean_tail = nil
      if practice.completed then
        if practice.deaths == 0 then
          clean_tail = math.floor(raw_elapsed)
        elseif practice.last_death_ms > 0 then
          -- Time from last save-state reload to completion
          -- last_death_ms is when the death happened; after reload, timer continued from start_ms
          -- We need: (completion_time - last_reload_time). The reload happens ~instantly after death.
          -- result_start_ms is when completion was detected.
          clean_tail = math.floor(practice.result_start_ms - practice.last_death_ms)
        end
      end

      local result = to_json({
        event        = "attempt_result",
        segment_id   = practice.segment.id,
        completed    = practice.completed,
        time_ms      = math.floor(practice.elapsed_ms),
        deaths       = practice.deaths,
        clean_tail_ms = clean_tail,
        goal         = practice.segment.goal,
      })
      if client then
        client:send(result .. "\n")
      end
      practice_reset()
      log("Practice: auto-advanced, sent result")
    end
  end
```

- [ ] **Step 5: Run fast tests to verify no Python regressions**

Run: `python -m pytest -m "not (emulator or slow or frontend)" -v`
Expected: ALL PASS (Lua changes don't affect unit tests)

- [ ] **Step 6: Commit**

```bash
git add lua/spinlab.lua
git commit -m "feat: add death penalty and death/clean_tail tracking to Lua practice mode"
```

---

### Task 4: Add `AttemptSource.REFERENCE` enum value

**Files:**
- Modify: `python/spinlab/models.py:80-83` (AttemptSource)
- Test: `tests/test_models_enums.py` (existing)

- [ ] **Step 1: Write failing test**

```python
# In tests/test_models_enums.py, add:
from spinlab.models import AttemptSource

def test_attempt_source_reference():
    assert AttemptSource.REFERENCE == "reference"
    assert AttemptSource.REFERENCE.value == "reference"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_models_enums.py::test_attempt_source_reference -v`
Expected: FAIL — `AttemptSource` has no `REFERENCE` member

- [ ] **Step 3: Add REFERENCE to AttemptSource**

In `python/spinlab/models.py`:

```python
class AttemptSource(StrEnum):
    PRACTICE = "practice"
    REPLAY = "replay"
    REFERENCE = "reference"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_models_enums.py::test_attempt_source_reference -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/models.py tests/test_models_enums.py
git commit -m "feat: add AttemptSource.REFERENCE enum value"
```

---

### Task 5: Add segment timing to ReferenceCapture

Track `timestamp_ms` on `pending_start`, count deaths between start/end, and compute per-segment timing when closing segments.

**Files:**
- Modify: `python/spinlab/reference_capture.py`
- Create: `tests/test_reference_capture.py`

- [ ] **Step 1: Write failing test for segment timing accumulation**

```python
# tests/test_reference_capture.py
import pytest
from unittest.mock import MagicMock
from spinlab.reference_capture import ReferenceCapture, RefSegmentTime
from spinlab.condition_registry import ConditionRegistry


@pytest.fixture
def db():
    """Minimal mock DB for reference capture tests."""
    mock = MagicMock()
    mock.upsert_waypoint = MagicMock()
    mock.upsert_segment = MagicMock()
    mock.add_save_state = MagicMock()
    mock.conn = MagicMock()
    mock.conn.execute = MagicMock(return_value=MagicMock(fetchone=MagicMock(return_value=None)))
    return mock


@pytest.fixture
def registry():
    return ConditionRegistry()


def test_clean_segment_timing(db, registry):
    """A segment with no deaths records correct time_ms and clean_tail_ms."""
    cap = ReferenceCapture()
    cap.capture_run_id = "run1"

    # Entrance at t=1000
    cap.handle_entrance({
        "level": 1, "state_path": "/s.mss",
        "conditions": {}, "timestamp_ms": 1000,
    })

    # Exit at t=6000 (5 seconds, no deaths)
    cap.handle_exit(
        {"level": 1, "goal": "normal", "conditions": {}, "timestamp_ms": 6000},
        game_id="g", db=db, registry=registry,
    )

    assert len(cap.segment_times) == 1
    st = cap.segment_times[0]
    assert st.time_ms == 5000
    assert st.deaths == 0
    assert st.clean_tail_ms == 5000


def test_segment_with_deaths_timing(db, registry):
    """A segment with deaths records correct time_ms and clean_tail_ms."""
    cap = ReferenceCapture()
    cap.capture_run_id = "run1"

    cap.handle_entrance({
        "level": 1, "state_path": "/s.mss",
        "conditions": {}, "timestamp_ms": 1000,
    })

    # Death at t=3000
    cap.handle_death(timestamp_ms=3000)
    # Spawn at t=6000
    cap.handle_spawn_timing(timestamp_ms=6000)

    # Exit at t=9000
    cap.handle_exit(
        {"level": 1, "goal": "normal", "conditions": {}, "timestamp_ms": 9000},
        game_id="g", db=db, registry=registry,
    )

    assert len(cap.segment_times) == 1
    st = cap.segment_times[0]
    assert st.time_ms == 8000  # 9000 - 1000
    assert st.deaths == 1
    assert st.clean_tail_ms == 3000  # 9000 - 6000


def test_checkpoint_splits_timing(db, registry):
    """Checkpoint closes one segment and starts the next with correct timing."""
    cap = ReferenceCapture()
    cap.capture_run_id = "run1"

    cap.handle_entrance({
        "level": 1, "state_path": "/s.mss",
        "conditions": {}, "timestamp_ms": 1000,
    })

    cap.handle_checkpoint(
        {"cp_ordinal": 1, "level_num": 1, "state_path": "/cp1.mss",
         "conditions": {}, "timestamp_ms": 4000},
        game_id="g", db=db, registry=registry,
    )

    cap.handle_exit(
        {"level": 1, "goal": "normal", "conditions": {}, "timestamp_ms": 7000},
        game_id="g", db=db, registry=registry,
    )

    assert len(cap.segment_times) == 2
    assert cap.segment_times[0].time_ms == 3000  # 4000 - 1000
    assert cap.segment_times[1].time_ms == 3000  # 7000 - 4000


def test_clear_resets_segment_times(db, registry):
    """clear() empties segment_times."""
    cap = ReferenceCapture()
    cap.capture_run_id = "run1"
    cap.handle_entrance({
        "level": 1, "state_path": "/s.mss",
        "conditions": {}, "timestamp_ms": 0,
    })
    cap.handle_exit(
        {"level": 1, "goal": "normal", "conditions": {}, "timestamp_ms": 5000},
        game_id="g", db=db, registry=registry,
    )
    assert len(cap.segment_times) == 1
    cap.clear()
    assert len(cap.segment_times) == 0


def test_abort_exit_no_timing(db, registry):
    """Aborted exits don't produce segment times."""
    cap = ReferenceCapture()
    cap.capture_run_id = "run1"
    cap.handle_entrance({
        "level": 1, "state_path": "/s.mss",
        "conditions": {}, "timestamp_ms": 0,
    })
    cap.handle_exit(
        {"level": 1, "goal": "abort", "conditions": {}, "timestamp_ms": 5000},
        game_id="g", db=db, registry=registry,
    )
    assert len(cap.segment_times) == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_reference_capture.py -v`
Expected: FAIL — `RefSegmentTime` not importable, `segment_times` not on ReferenceCapture

- [ ] **Step 3: Implement RefSegmentTime and timing in ReferenceCapture**

In `python/spinlab/reference_capture.py`:

Add dataclass at top of file (after imports):

```python
from dataclasses import dataclass

@dataclass
class RefSegmentTime:
    """Timing data for one segment captured during a reference run."""
    segment_id: str
    time_ms: int
    deaths: int
    clean_tail_ms: int
```

Update `__init__` and `clear`:

```python
def __init__(self) -> None:
    self.segments_count: int = 0
    self.capture_run_id: str | None = None
    self.pending_start: dict | None = None
    self.died: bool = False
    self.rec_path: str | None = None
    self.segment_times: list[RefSegmentTime] = []
    self._deaths_in_segment: int = 0
    self._last_spawn_ms: int | None = None

def clear(self) -> None:
    """Reset all capture state."""
    self.segments_count = 0
    self.capture_run_id = None
    self.pending_start = None
    self.died = False
    self.rec_path = None
    self.segment_times = []
    self._deaths_in_segment = 0
    self._last_spawn_ms = None
```

Update `handle_entrance` to store `timestamp_ms` on pending_start and reset death tracking:

```python
def handle_entrance(self, event: dict) -> None:
    if self.pending_start and self.pending_start["type"] != "entrance":
        logger.info("Ignoring level_entrance — pending start exists: %s",
                    self.pending_start)
        return
    self.pending_start = {
        "type": "entrance",
        "ordinal": 0,
        "state_path": event.get("state_path"),
        "timestamp_ms": event.get("timestamp_ms", 0),
        "level_num": event["level"],
        "raw_conditions": event.get("conditions", {}),
    }
    self.died = False
    self._deaths_in_segment = 0
    self._last_spawn_ms = None
```

Add timing-only death and spawn handlers:

```python
def handle_death(self, timestamp_ms: int | None = None) -> None:
    """Track a death for segment timing. Also sets died flag."""
    self.died = True
    self._deaths_in_segment += 1

def handle_spawn_timing(self, timestamp_ms: int | None = None) -> None:
    """Track spawn timestamp for clean_tail_ms computation."""
    if timestamp_ms is not None:
        self._last_spawn_ms = timestamp_ms
```

Update `_close_segment` to record timing. Add `end_timestamp_ms` parameter:

```python
def _close_segment(self, db, game_id, start, end_type, end_ordinal,
                   level, end_raw_conditions, registry,
                   end_timestamp_ms: int | None = None) -> None:
    """Create waypoints + segment for the segment ending here."""
    from .models import Segment, Waypoint, WaypointSaveState

    # ... existing waypoint/segment creation code unchanged ...

    # Record timing if timestamps are available
    start_ts = start.get("timestamp_ms")
    if start_ts is not None and end_timestamp_ms is not None:
        time_ms = end_timestamp_ms - start_ts
        deaths = self._deaths_in_segment
        if deaths == 0:
            clean_tail_ms = time_ms
        elif self._last_spawn_ms is not None:
            clean_tail_ms = end_timestamp_ms - self._last_spawn_ms
        else:
            clean_tail_ms = time_ms  # fallback: no spawn timestamp available
        self.segment_times.append(RefSegmentTime(
            segment_id=seg_id,
            time_ms=time_ms,
            deaths=deaths,
            clean_tail_ms=clean_tail_ms,
        ))

    # Reset death tracking for next segment
    self._deaths_in_segment = 0
    self._last_spawn_ms = None
```

Update `handle_checkpoint` to pass `end_timestamp_ms` and reset death state for next segment:

```python
def handle_checkpoint(self, event: dict, game_id: str,
                      db: "Database",
                      registry: "ConditionRegistry") -> None:
    if not self.pending_start:
        return
    cp_ordinal = event.get("cp_ordinal", 1)
    level = event.get("level_num", self.pending_start["level_num"])
    end_ts = event.get("timestamp_ms")
    self._close_segment(
        db, game_id, self.pending_start, "checkpoint", cp_ordinal,
        level, event.get("conditions", {}), registry,
        end_timestamp_ms=end_ts)
    self.pending_start = {
        "type": "checkpoint",
        "ordinal": cp_ordinal,
        "state_path": event.get("state_path"),
        "timestamp_ms": event.get("timestamp_ms", 0),
        "level_num": level,
        "raw_conditions": event.get("conditions", {}),
    }
```

Update `handle_exit` to pass `end_timestamp_ms`:

```python
def handle_exit(self, event: dict, game_id: str,
                db: "Database",
                registry: "ConditionRegistry") -> None:
    goal = event.get("goal", "abort")
    if goal == "abort":
        self.pending_start = None
        return
    if not self.pending_start:
        return
    level = event["level"]
    end_ts = event.get("timestamp_ms")
    self._close_segment(
        db, game_id, self.pending_start, "goal", 0,
        level, event.get("conditions", {}), registry,
        end_timestamp_ms=end_ts)
    self.pending_start = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_reference_capture.py -v`
Expected: ALL PASS

- [ ] **Step 5: Run full fast test suite for regressions**

Run: `python -m pytest -m "not (emulator or slow or frontend)" -v`
Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/reference_capture.py tests/test_reference_capture.py
git commit -m "feat: accumulate per-segment timing during reference capture"
```

---

### Task 6: Wire death/spawn events into ReferenceCapture timing

The `CaptureController` already calls `ref_capture.handle_entrance`, `handle_checkpoint`, `handle_exit`, and sets `ref_capture.died = True`. Now route `timestamp_ms` from death and spawn events into the timing methods.

**Files:**
- Modify: `python/spinlab/capture_controller.py:200-213`
- Modify: `python/spinlab/session_manager.py:248-270`

- [ ] **Step 1: Write failing test**

```python
# tests/test_reference_capture.py — add:
def test_death_via_capture_controller_increments_deaths(db, registry):
    """handle_death on ReferenceCapture increments death counter."""
    cap = ReferenceCapture()
    cap.capture_run_id = "run1"
    cap.handle_entrance({
        "level": 1, "state_path": "/s.mss",
        "conditions": {}, "timestamp_ms": 1000,
    })
    cap.handle_death(timestamp_ms=2000)
    cap.handle_death(timestamp_ms=3000)
    assert cap._deaths_in_segment == 2
```

- [ ] **Step 2: Run test to verify it passes**

This test should already pass from Task 5's implementation. Verify:

Run: `python -m pytest tests/test_reference_capture.py::test_death_via_capture_controller_increments_deaths -v`
Expected: PASS

- [ ] **Step 3: Update CaptureController.handle_death to pass timestamp**

In `python/spinlab/capture_controller.py`:

```python
def handle_death(self, event: dict | None = None) -> None:
    self.ref_capture.died = True
    ts = event.get("timestamp_ms") if event else None
    self.ref_capture.handle_death(timestamp_ms=ts)
```

- [ ] **Step 4: Update CaptureController.handle_spawn to pass timing**

In `python/spinlab/capture_controller.py`, update `handle_spawn`:

```python
def handle_spawn(self, event: dict, game_id: str) -> None:
    logger.info("capture: spawn level=%s state_captured=%s",
                event.get("level_num"), event.get("state_captured"))
    self.ref_capture.handle_spawn_timing(timestamp_ms=event.get("timestamp_ms"))
    self.ref_capture.handle_spawn(event, game_id, self.db,
                                  self.condition_registry)
```

- [ ] **Step 5: Update SessionManager._handle_death to pass event dict**

In `python/spinlab/session_manager.py`, update `_handle_death`:

```python
async def _handle_death(self, event: DeathEvent) -> None:
    if self.mode not in (Mode.REFERENCE, Mode.REPLAY, Mode.COLD_FILL):
        return
    if self.mode == Mode.COLD_FILL:
        logger.info("death during cold_fill — waiting for respawn")
    if self.mode in (Mode.REFERENCE, Mode.REPLAY):
        self.capture.handle_death(dataclasses.asdict(event))
```

- [ ] **Step 6: Run fast tests**

Run: `python -m pytest -m "not (emulator or slow or frontend)" -v`
Expected: ALL PASS

- [ ] **Step 7: Commit**

```bash
git add python/spinlab/capture_controller.py python/spinlab/session_manager.py tests/test_reference_capture.py
git commit -m "feat: route death/spawn timestamps into reference capture timing"
```

---

### Task 7: Seed attempts on draft save

Insert reference-sourced attempts into the DB and rebuild estimator states when a draft is saved.

**Files:**
- Modify: `python/spinlab/draft_manager.py:29-37` (save method)
- Modify: `python/spinlab/capture_controller.py:245-248` (save_draft)
- Modify: `python/spinlab/session_manager.py:353-359` (save_draft)
- Test: `tests/test_reference_seeding.py` (new)

- [ ] **Step 1: Write failing test for seed attempt insertion**

```python
# tests/test_reference_seeding.py
import pytest
from spinlab.db import Database
from spinlab.models import Attempt, AttemptSource, Segment, Waypoint, WaypointSaveState
from spinlab.reference_capture import RefSegmentTime


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    d.upsert_game("g", "Game", "any%")
    return d


def _make_segment(db, seg_id, game_id="g", level=1, ref_id="run1"):
    wp_s = Waypoint.make(game_id, level, "entrance", 0, {})
    wp_e = Waypoint.make(game_id, level, "goal", 0, {})
    db.upsert_waypoint(wp_s)
    db.upsert_waypoint(wp_e)
    seg = Segment(
        id=seg_id, game_id=game_id, level_number=level,
        start_type="entrance", start_ordinal=0,
        end_type="goal", end_ordinal=0,
        reference_id=ref_id,
        start_waypoint_id=wp_s.id, end_waypoint_id=wp_e.id,
    )
    db.upsert_segment(seg)
    return seg


def test_seed_attempts_inserted(db):
    """Seeding inserts reference attempts into the attempts table."""
    from spinlab.reference_seeding import seed_reference_attempts

    db.create_capture_run("run1", "g", "Test Run", draft=True)
    _make_segment(db, "seg1")
    _make_segment(db, "seg2", level=2)

    times = [
        RefSegmentTime(segment_id="seg1", time_ms=5000, deaths=0, clean_tail_ms=5000),
        RefSegmentTime(segment_id="seg2", time_ms=8000, deaths=1, clean_tail_ms=3000),
    ]

    seed_reference_attempts(db, "run1", times)

    attempts = db.get_segment_attempts("seg1")
    assert len(attempts) == 1
    assert attempts[0]["time_ms"] == 5000
    assert attempts[0]["deaths"] == 0
    assert attempts[0]["clean_tail_ms"] == 5000

    attempts2 = db.get_segment_attempts("seg2")
    assert len(attempts2) == 1
    assert attempts2[0]["time_ms"] == 8000
    assert attempts2[0]["deaths"] == 1
    assert attempts2[0]["clean_tail_ms"] == 3000


def test_seed_attempts_source_is_reference(db):
    """Seeded attempts have source='reference'."""
    from spinlab.reference_seeding import seed_reference_attempts

    db.create_capture_run("run1", "g", "Test Run", draft=True)
    _make_segment(db, "seg1")

    times = [RefSegmentTime(segment_id="seg1", time_ms=5000, deaths=0, clean_tail_ms=5000)]
    seed_reference_attempts(db, "run1", times)

    row = db.conn.execute(
        "SELECT source FROM attempts WHERE segment_id = ?", ("seg1",)
    ).fetchone()
    assert row[0] == "reference"


def test_seed_with_empty_times(db):
    """Seeding with no times is a no-op."""
    from spinlab.reference_seeding import seed_reference_attempts

    db.create_capture_run("run1", "g", "Test Run", draft=True)
    seed_reference_attempts(db, "run1", [])

    row = db.conn.execute("SELECT COUNT(*) FROM attempts").fetchone()
    assert row[0] == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_reference_seeding.py -v`
Expected: FAIL — `spinlab.reference_seeding` module not found

- [ ] **Step 3: Create reference_seeding module**

Create `python/spinlab/reference_seeding.py`:

```python
"""Insert reference run segment times as seed attempts."""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from .models import Attempt, AttemptSource

if TYPE_CHECKING:
    from .db import Database
    from .reference_capture import RefSegmentTime

logger = logging.getLogger(__name__)


def seed_reference_attempts(
    db: "Database",
    capture_run_id: str,
    segment_times: list["RefSegmentTime"],
) -> int:
    """Insert seed attempts from reference segment times.

    Returns the number of attempts inserted.
    """
    if not segment_times:
        return 0

    now = datetime.now(UTC)
    count = 0
    for rst in segment_times:
        attempt = Attempt(
            segment_id=rst.segment_id,
            session_id=capture_run_id,
            completed=True,
            time_ms=rst.time_ms,
            deaths=rst.deaths,
            clean_tail_ms=rst.clean_tail_ms,
            source=AttemptSource.REFERENCE,
            created_at=now,
        )
        db.log_attempt(attempt)
        count += 1
        logger.info("seed: segment=%s time=%dms deaths=%d clean_tail=%dms",
                     rst.segment_id, rst.time_ms, rst.deaths, rst.clean_tail_ms)

    return count
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_reference_seeding.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/reference_seeding.py tests/test_reference_seeding.py
git commit -m "feat: add reference_seeding module for inserting seed attempts"
```

---

### Task 8: Integrate seeding into the draft save flow

Wire `seed_reference_attempts` and `rebuild_all_states` into the existing draft save path.

**Files:**
- Modify: `python/spinlab/draft_manager.py:29-37`
- Modify: `python/spinlab/capture_controller.py:245-248`
- Modify: `python/spinlab/session_manager.py:353-359`
- Test: `tests/test_reference_seeding.py` (extend)

- [ ] **Step 1: Write failing integration test for draft save seeding**

```python
# tests/test_reference_seeding.py — add:
def test_draft_save_seeds_and_rebuilds(db):
    """Full flow: DraftManager.save() triggers seeding + estimator rebuild."""
    from unittest.mock import MagicMock, patch
    from spinlab.draft_manager import DraftManager
    from spinlab.reference_capture import RefSegmentTime

    db.create_capture_run("run1", "g", "Draft", draft=True)
    _make_segment(db, "seg1")

    times = [RefSegmentTime(segment_id="seg1", time_ms=5000, deaths=0, clean_tail_ms=5000)]

    dm = DraftManager()
    dm.enter_draft("run1", 1)

    mock_scheduler = MagicMock()
    result = dm.save(db, "Saved Run", segment_times=times, scheduler=mock_scheduler)

    assert result.status.value == "ok"

    # Verify attempt was inserted
    attempts = db.get_segment_attempts("seg1")
    assert len(attempts) == 1
    assert attempts[0]["time_ms"] == 5000

    # Verify rebuild was called
    mock_scheduler.rebuild_all_states.assert_called_once()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_reference_seeding.py::test_draft_save_seeds_and_rebuilds -v`
Expected: FAIL — `save() got unexpected keyword argument 'segment_times'`

- [ ] **Step 3: Update DraftManager.save to accept segment_times and scheduler**

In `python/spinlab/draft_manager.py`:

```python
from __future__ import annotations

from typing import TYPE_CHECKING

from .models import ActionResult, Status

if TYPE_CHECKING:
    from .db import Database
    from .reference_capture import RefSegmentTime
    from .scheduler import Scheduler


class DraftManager:
    """Manages draft capture runs (pending save/discard after recording or replay)."""

    def __init__(self) -> None:
        self.run_id: str | None = None
        self.segments_count: int = 0

    # ... has_draft, enter_draft unchanged ...

    def save(
        self, db: "Database", name: str,
        segment_times: list["RefSegmentTime"] | None = None,
        scheduler: "Scheduler | None" = None,
    ) -> ActionResult:
        """Promote draft capture run to saved reference, seed attempts, rebuild model."""
        if not self.run_id:
            return ActionResult(status=Status.NO_DRAFT)
        db.promote_draft(self.run_id, name)
        db.set_active_capture_run(self.run_id)

        # Seed reference attempts if timing data is available
        if segment_times:
            from .reference_seeding import seed_reference_attempts
            seed_reference_attempts(db, self.run_id, segment_times)
            if scheduler:
                scheduler.rebuild_all_states()

        self.run_id = None
        self.segments_count = 0
        return ActionResult(status=Status.OK)

    # ... discard, recover, get_state unchanged ...
```

- [ ] **Step 4: Update CaptureController.save_draft to pass segment_times**

In `python/spinlab/capture_controller.py`:

```python
async def save_draft(self, name: str, scheduler=None) -> ActionResult:
    return self.draft.save(
        self.db, name,
        segment_times=self.ref_capture.segment_times or None,
        scheduler=scheduler,
    )
```

- [ ] **Step 5: Update SessionManager.save_draft to pass scheduler**

In `python/spinlab/session_manager.py`:

```python
async def save_draft(self, name: str) -> ActionResult:
    scheduler = self._get_scheduler() if self.game_id else None
    result = await self.capture.save_draft(name, scheduler=scheduler)
    if result.status == Status.OK and self.game_id and self.tcp.is_connected:
        cf_result = await self.cold_fill.start(self.game_id)
        if cf_result.new_mode == Mode.COLD_FILL:
            self.mode = Mode.COLD_FILL
    await self._notify_sse()
    return result
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/test_reference_seeding.py -v`
Expected: ALL PASS

- [ ] **Step 7: Run full fast test suite**

Run: `python -m pytest -m "not (emulator or slow or frontend)" -v`
Expected: ALL PASS

- [ ] **Step 8: Commit**

```bash
git add python/spinlab/draft_manager.py python/spinlab/capture_controller.py python/spinlab/session_manager.py tests/test_reference_seeding.py
git commit -m "feat: integrate reference seeding into draft save flow"
```

---

### Task 9: Full test suite verification

Run the complete test suite to verify no regressions across all test layers.

**Files:** None (verification only)

- [ ] **Step 1: Run full pytest**

Run: `python -m pytest -v`
Expected: ALL PASS

- [ ] **Step 2: Run frontend tests**

Run: `cd frontend && npm test`
Expected: ALL PASS

- [ ] **Step 3: Fix any failures**

If any tests fail, investigate and fix. Re-run until all pass.

- [ ] **Step 4: Final commit if any fixes were needed**

```bash
git add -A
git commit -m "fix: address test failures from ref-as-seed implementation"
```
