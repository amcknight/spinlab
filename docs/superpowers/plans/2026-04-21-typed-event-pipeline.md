# Typed Event Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove all `dataclasses.asdict()` conversions on the event path, passing typed protocol events all the way from SessionManager to every downstream consumer.

**Architecture:** SessionManager already receives typed events from `parse_event()`. We remove the dict round-trip by passing those typed events directly to ReferenceController, SegmentRecorder, ColdFillController, PracticeSession, and SpeedRunSession. We also replace `SegmentRecorder.pending_start: dict` with a typed `PendingStart` dataclass. SpeedRunSession's single `receive_event(dict)` splits into three typed methods.

**Tech Stack:** Python 3.11+ dataclasses, existing protocol event types from `spinlab.protocol`

**Spec:** `docs/superpowers/specs/2026-04-21-typed-event-pipeline-design.md`

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `python/spinlab/capture/recorder.py` | Modify | Add `PendingStart` dataclass, change all handler signatures to typed events |
| `python/spinlab/capture/reference.py` | Modify | Change all handler signatures to typed events |
| `python/spinlab/capture/cold_fill.py` | Modify | Change `handle_spawn` to accept `SpawnEvent` |
| `python/spinlab/practice.py` | Modify | Change `receive_result` and `_process_result` to use `AttemptResultEvent` |
| `python/spinlab/speed_run.py` | Modify | Split `receive_event` into 3 typed methods, add `SpeedRunEvent` alias |
| `python/spinlab/session_manager.py` | Modify | Remove all `dataclasses.asdict()` calls, pass typed events |
| `tests/unit/capture/test_recorder.py` | Modify | Use typed events instead of dicts |
| `tests/unit/capture/test_capture_with_conditions.py` | Modify | Use typed events instead of dicts |
| `tests/unit/capture/test_cold_fill.py` | Modify | Use `SpawnEvent` instead of dicts |
| `tests/unit/capture/test_draft.py` | Modify | Remove `asdict()` in seeding regression test |
| `tests/unit/test_practice.py` | Modify | Use `AttemptResultEvent` instead of dicts |
| `tests/unit/test_practice_coverage.py` | Modify | Use `AttemptResultEvent` instead of dicts |
| `tests/unit/test_speed_run_mode.py` | Modify | Use typed events instead of dicts |

---

### Task 1: Add `PendingStart` dataclass and type `SegmentRecorder`

The recorder is the deepest layer — type it first so upstream layers can be updated against passing tests.

**Files:**
- Modify: `python/spinlab/capture/recorder.py`
- Modify: `tests/unit/capture/test_recorder.py`

- [ ] **Step 1: Add `PendingStart` dataclass to recorder.py**

Add at the top of `recorder.py`, after the existing imports:

```python
from ..protocol import (
    CheckpointEvent,
    LevelEntranceEvent,
    LevelExitEvent,
    SpawnEvent,
)


@dataclass
class PendingStart:
    """Buffered start-of-segment state for pairing with the next endpoint."""
    type: str              # "entrance" or "checkpoint"
    ordinal: int
    state_path: str | None
    timestamp_ms: int
    level_num: int
    raw_conditions: dict
```

Add `dataclass` to the existing `from dataclasses import dataclass` import (it's already there for `RecordedSegmentTime`).

- [ ] **Step 2: Change `pending_start` type annotation and `handle_entrance` signature**

Change `self.pending_start: dict | None = None` in `__init__` and `clear()` to `self.pending_start: PendingStart | None = None`.

Change `handle_entrance(self, event: dict)` to `handle_entrance(self, event: LevelEntranceEvent)`:

```python
def handle_entrance(self, event: LevelEntranceEvent) -> None:
    """Buffer a level entrance as pending start."""
    if self.pending_start and self.pending_start.type != "entrance":
        logger.info("Ignoring level_entrance — pending start exists: %s",
                    self.pending_start)
        return
    self.pending_start = PendingStart(
        type="entrance",
        ordinal=0,
        state_path=event.state_path,
        timestamp_ms=event.timestamp_ms,
        level_num=event.level,
        raw_conditions=event.conditions,
    )
    self.died = False
    self._deaths_in_segment = 0
    self._last_spawn_ms = None
```

- [ ] **Step 3: Change `handle_checkpoint` signature**

Change `handle_checkpoint(self, event: dict, game_id: str, db, registry)` to `handle_checkpoint(self, event: CheckpointEvent, game_id: str, db, registry)`:

```python
def handle_checkpoint(self, event: CheckpointEvent, game_id: str,
                      db: "Database",
                      registry: "ConditionRegistry") -> None:
    """Close current segment at checkpoint, start new one."""
    if not self.pending_start:
        return
    cp_ordinal = event.cp_ordinal
    level = event.level_num if event.level_num else self.pending_start.level_num
    self._close_segment(
        db, game_id, self.pending_start, "checkpoint", cp_ordinal,
        level, event.conditions, registry,
        end_timestamp_ms=event.timestamp_ms)
    self.pending_start = PendingStart(
        type="checkpoint",
        ordinal=cp_ordinal,
        state_path=event.state_path,
        timestamp_ms=event.timestamp_ms,
        level_num=level,
        raw_conditions=event.conditions,
    )
```

- [ ] **Step 4: Change `handle_exit` signature**

Change `handle_exit(self, event: dict, game_id, db, registry)` to `handle_exit(self, event: LevelExitEvent, game_id, db, registry)`:

```python
def handle_exit(self, event: LevelExitEvent, game_id: str,
                db: "Database",
                registry: "ConditionRegistry") -> None:
    """Pair level_exit with pending start to create final segment."""
    if event.goal == "abort":
        self.pending_start = None
        return
    if not self.pending_start:
        return
    level = event.level
    self._close_segment(
        db, game_id, self.pending_start, "goal", 0,
        level, event.conditions, registry,
        end_timestamp_ms=event.timestamp_ms)
    self.pending_start = None
```

- [ ] **Step 5: Change `handle_spawn` signature**

Change `handle_spawn(self, event: dict, game_id, db, registry)` to `handle_spawn(self, event: SpawnEvent, game_id, db, registry)`:

```python
def handle_spawn(self, event: SpawnEvent, game_id: str,
                 db: "Database",
                 registry: "ConditionRegistry") -> None:
    """Store cold save state on checkpoint waypoint after a respawn."""
    if not event.is_cold_cp or not event.state_captured:
        return
    cold_path = event.state_path
    level = event.level_num
    cp_ord = event.cp_ordinal
    if cold_path is None or cp_ord is None:
        return
    from ..models import EndpointType, Waypoint, WaypointSaveState
    conds = registry.decode(event.conditions, level=level)
    wp = Waypoint.make(game_id, level, EndpointType.CHECKPOINT, cp_ord, conds)
    db.upsert_waypoint(wp)
    db.add_save_state(WaypointSaveState(
        waypoint_id=wp.id, variant_type="cold",
        state_path=cold_path, is_default=True))
    logger.debug("Stored cold save state for waypoint %s: %s", wp.id, cold_path)
```

- [ ] **Step 6: Update `_close_segment` to use `PendingStart` attributes**

The `start` parameter is already `PendingStart` (typed via callers). Update attribute access from dict to dataclass:

Change all `start["type"]` to `start.type`, `start["ordinal"]` to `start.ordinal`, `start["raw_conditions"]` to `start.raw_conditions`, `start["level_num"]` to `start.level_num`, `start.get("state_path")` to `start.state_path`, `start.get("timestamp_ms")` to `start.timestamp_ms`.

The full updated `_close_segment` signature and body:

```python
def _close_segment(self, db, game_id, start: PendingStart, end_type, end_ordinal,
                   level, end_raw_conditions, registry,
                   end_timestamp_ms: int | None = None) -> None:
    """Create waypoints + segment for the segment ending here."""
    from ..models import Segment, Waypoint, WaypointSaveState

    start_conds = registry.decode(start.raw_conditions, level=level)
    end_conds = registry.decode(end_raw_conditions, level=level)

    wp_start = Waypoint.make(game_id, level, start.type,
                             start.ordinal, start_conds)
    wp_end = Waypoint.make(game_id, level, end_type, end_ordinal, end_conds)
    db.upsert_waypoint(wp_start)
    db.upsert_waypoint(wp_end)

    seg_id = Segment.make_id(
        game_id, level, start.type, start.ordinal,
        end_type, end_ordinal, wp_start.id, wp_end.id,
    )
    is_primary = self._compute_is_primary(
        db, game_id, level, start.type, start.ordinal,
        end_type, end_ordinal, seg_id)
    self.segments_count += 1
    seg = Segment(
        id=seg_id, game_id=game_id, level_number=level,
        start_type=start.type, start_ordinal=start.ordinal,
        end_type=end_type, end_ordinal=end_ordinal,
        start_waypoint_id=wp_start.id, end_waypoint_id=wp_end.id,
        is_primary=is_primary,
        ordinal=self.segments_count,
        reference_id=self.capture_run_id,
    )
    db.upsert_segment(seg)

    if start.state_path:
        variant = "cold" if start.type == "entrance" else "hot"
        db.add_save_state(WaypointSaveState(
            waypoint_id=wp_start.id,
            variant_type=variant,
            state_path=start.state_path,
            is_default=True,
        ))

    # Record timing if timestamps are available
    if start.timestamp_ms is not None and end_timestamp_ms is not None:
        time_ms = end_timestamp_ms - start.timestamp_ms
        deaths = self._deaths_in_segment
        if deaths == 0:
            clean_tail_ms = time_ms
        elif self._last_spawn_ms is not None:
            clean_tail_ms = end_timestamp_ms - self._last_spawn_ms
        else:
            clean_tail_ms = time_ms  # fallback
        self.segment_times.append(RecordedSegmentTime(
            segment_id=seg_id,
            time_ms=time_ms,
            deaths=deaths,
            clean_tail_ms=clean_tail_ms,
        ))

    # Reset death tracking for next segment
    self._deaths_in_segment = 0
    self._last_spawn_ms = None
```

- [ ] **Step 7: Update test_recorder.py to use typed events**

Replace all dict event construction with protocol dataclasses. Add imports at top:

```python
from spinlab.protocol import LevelEntranceEvent, LevelExitEvent, CheckpointEvent
```

Then replace every `{"level": N, ...}` dict with the corresponding typed event. Examples of each pattern:

Entrance events: `{"level": 1, "timestamp_ms": 1000, "state_path": "/s.mss"}` becomes `LevelEntranceEvent(level=1, timestamp_ms=1000, state_path="/s.mss")`

Exit events: `{"level": 1, "goal": "goal", "timestamp_ms": 6000}` becomes `LevelExitEvent(level=1, goal="goal", timestamp_ms=6000)`

Checkpoint events: `{"level_num": 1, "cp_ordinal": 1, "timestamp_ms": 4000}` becomes `CheckpointEvent(level_num=1, cp_ordinal=1, timestamp_ms=4000)`

The `handle_death` calls in this file already use the `timestamp_ms=` keyword directly (not a dict), so they don't change.

- [ ] **Step 8: Update test_capture_with_conditions.py to use typed events**

Add import at top:

```python
from spinlab.protocol import LevelEntranceEvent, LevelExitEvent
```

Replace entrance dicts like `{"level": 5, "state_path": "/tmp/start.mss", "conditions": {"powerup": 0}}` with `LevelEntranceEvent(level=5, state_path="/tmp/start.mss", conditions={"powerup": 0})`.

Replace exit dicts like `{"level": 5, "goal": "goal", "conditions": {"powerup": 0}}` with `LevelExitEvent(level=5, goal="goal", conditions={"powerup": 0})`.

- [ ] **Step 9: Run tests to verify**

Run: `python -m pytest tests/unit/capture/test_recorder.py tests/unit/capture/test_capture_with_conditions.py -v`

Expected: All tests pass.

- [ ] **Step 10: Commit**

```
feat: type SegmentRecorder event handlers and add PendingStart dataclass

Replace dict-based event handling in SegmentRecorder with typed protocol
events (LevelEntranceEvent, CheckpointEvent, LevelExitEvent, SpawnEvent).
Replace pending_start: dict with PendingStart dataclass.
```

---

### Task 2: Type `ReferenceController` handlers

Wire the typed events through the middle layer. SegmentRecorder already accepts typed events from Task 1.

**Files:**
- Modify: `python/spinlab/capture/reference.py`

- [ ] **Step 1: Add protocol imports to reference.py**

Add to the existing imports from `..protocol`:

```python
from ..protocol import (
    SPEED_UNCAPPED,
    CheckpointEvent,
    DeathEvent,
    FillGapLoadCmd,
    LevelEntranceEvent,
    LevelExitEvent,
    RecSavedEvent,
    ReferenceStartCmd,
    ReferenceStopCmd,
    ReplayCmd,
    ReplayStopCmd,
    SpawnEvent,
)
```

- [ ] **Step 2: Update `handle_entrance`**

```python
def handle_entrance(self, event: LevelEntranceEvent) -> None:
    logger.info("capture: entrance level=%s", event.level)
    self.recorder.handle_entrance(event)
```

- [ ] **Step 3: Update `handle_checkpoint`**

```python
def handle_checkpoint(self, event: CheckpointEvent, game_id: str) -> None:
    logger.info("capture: checkpoint level=%s cp=%s",
                 event.level_num, event.cp_ordinal)
    self.recorder.handle_checkpoint(event, game_id, self.db,
                                       self.condition_registry)
```

- [ ] **Step 4: Update `handle_death`**

`DeathEvent` has no `timestamp_ms` field — the old code tried `event.get("timestamp_ms")` which always returned `None`. Remove the dead extraction:

```python
def handle_death(self, event: DeathEvent) -> None:
    self.recorder.died = True
    self.recorder.handle_death(timestamp_ms=None)
```

- [ ] **Step 5: Update `handle_spawn`**

```python
def handle_spawn(self, event: SpawnEvent, game_id: str) -> None:
    logger.info("capture: spawn level=%s state_captured=%s",
                 event.level_num, event.state_captured)
    self.recorder.handle_spawn_timing(timestamp_ms=None)
    self.recorder.handle_spawn(event, game_id, self.db,
                                  self.condition_registry)
```

Note: `SpawnEvent` doesn't have `timestamp_ms` either — the old code passed `event.get("timestamp_ms")` which was also `None`. Clean this up the same way.

- [ ] **Step 6: Update `handle_exit`**

```python
def handle_exit(self, event: LevelExitEvent, game_id: str) -> None:
    logger.info("capture: exit level=%s segments_so_far=%d",
                 event.level, self.recorder.segments_count)
    self.recorder.handle_exit(event, game_id, self.db,
                                 self.condition_registry)
```

- [ ] **Step 7: Update `handle_rec_saved`**

```python
def handle_rec_saved(self, event: RecSavedEvent) -> None:
    self.recorder.rec_path = event.path
```

- [ ] **Step 8: Run tests to verify**

Run: `python -m pytest tests/unit/capture/ -v`

Expected: All capture tests pass (recorder tests already use typed events from Task 1, and draft tests don't call these handlers directly).

- [ ] **Step 9: Commit**

```
feat: type ReferenceController event handlers

Pass typed protocol events through ReferenceController to SegmentRecorder.
Remove dead timestamp_ms extraction from handle_death (DeathEvent has no
such field).
```

---

### Task 3: Type `ColdFillController.handle_spawn`

**Files:**
- Modify: `python/spinlab/capture/cold_fill.py`
- Modify: `tests/unit/capture/test_cold_fill.py`

- [ ] **Step 1: Update cold_fill.py**

Add import:

```python
from ..protocol import ColdFillLoadCmd, SpawnEvent
```

Remove `ColdFillLoadCmd` from the existing import line (it's being consolidated).

Change `handle_spawn(self, event: dict)` to:

```python
async def handle_spawn(self, event: SpawnEvent) -> bool:
    """Store cold save state, advance queue. Returns True when all done."""
    if not self.current:
        logger.warning("cold_fill: spawn received but no current segment")
        return False
    if not event.state_captured:
        logger.info("cold_fill: spawn without state_captured — ignoring (state_path=%s)",
                    event.state_path)
        return False
    logger.info("cold_fill: captured cold state for segment=%s path=%s",
                 self.current, event.state_path)
    if self.cold_waypoint_id:
        self.db.add_save_state(WaypointSaveState(
            waypoint_id=self.cold_waypoint_id,
            variant_type="cold",
            state_path=event.state_path,
            is_default=True,
        ))
    self.queue.pop(0)
    if not self.queue:
        logger.info("cold_fill: complete — all %d cold states captured", self.total)
        self.current = None
        self.cold_waypoint_id = None
        return True
    await self._load_next()
    return False
```

- [ ] **Step 2: Update test_cold_fill.py to use SpawnEvent**

Add import at top:

```python
from spinlab.protocol import ColdFillLoadCmd, SpawnEvent
```

Replace all `handle_spawn({"state_captured": True, "state_path": "/cold1.mss"})` calls with `handle_spawn(SpawnEvent(state_captured=True, state_path="/cold1.mss"))`.

Replace `handle_spawn({"state_captured": False})` with `handle_spawn(SpawnEvent(state_captured=False))`.

There are 5 `handle_spawn` calls in `test_cold_fill.py` that need updating.

- [ ] **Step 3: Run tests to verify**

Run: `python -m pytest tests/unit/capture/test_cold_fill.py -v`

Expected: All tests pass.

- [ ] **Step 4: Commit**

```
feat: type ColdFillController.handle_spawn to accept SpawnEvent
```

---

### Task 4: Type `PracticeSession`

**Files:**
- Modify: `python/spinlab/practice.py`
- Modify: `tests/unit/test_practice.py`
- Modify: `tests/unit/test_practice_coverage.py`

- [ ] **Step 1: Update practice.py**

Add import:

```python
from .protocol import AttemptResultEvent, PracticeLoadCmd, PracticeStopCmd
```

Remove `PracticeLoadCmd` and `PracticeStopCmd` from the existing import (consolidating).

Change `receive_result` signature and `_result_data` type:

```python
self._result_data: AttemptResultEvent | None = None
```

```python
def receive_result(self, event: AttemptResultEvent) -> None:
    """Called by SessionManager.route_event when attempt_result arrives."""
    self._result_data = event
    self._result_event.set()
```

- [ ] **Step 2: Update the guard in `run_one`**

Change the guard from:

```python
if self._result_data and self._result_data.get("event") == "attempt_result":
    self._process_result(self._result_data, cmd)
```

To:

```python
if self._result_data is not None:
    self._process_result(self._result_data, cmd)
```

- [ ] **Step 3: Update `_process_result` to use typed event**

Change signature from `_process_result(self, result: dict, cmd: SegmentCommand)` to `_process_result(self, result: AttemptResultEvent, cmd: SegmentCommand)`:

```python
def _process_result(self, result: AttemptResultEvent, cmd: SegmentCommand) -> None:
    attempt = Attempt(
        segment_id=result.segment_id,
        session_id=self.session_id,
        completed=result.completed,
        time_ms=result.time_ms,
        deaths=result.deaths,
        clean_tail_ms=result.clean_tail_ms,
        source=AttemptSource.PRACTICE,
        chosen_allocator=self._last_allocator,
    )
    self.db.log_attempt(attempt)
    self.scheduler.process_attempt(
        result.segment_id,
        time_ms=result.time_ms or 0,
        completed=result.completed,
        deaths=result.deaths,
        clean_tail_ms=result.clean_tail_ms,
    )
    self.segments_attempted += 1
    if result.completed:
        self.segments_completed += 1
    logger.info("practice: attempt segment=%s completed=%s time=%s deaths=%d",
                 result.segment_id, result.completed,
                 result.time_ms, result.deaths)
    if self.on_attempt:
        self.on_attempt(attempt)
```

- [ ] **Step 4: Update test_practice.py**

Add import:

```python
from spinlab.protocol import AttemptResultEvent
```

Replace all `receive_result({...})` dict calls with `receive_result(AttemptResultEvent(...))`.

There are 4 `receive_result` calls in this file. Example transformation:

```python
# Before:
session.receive_result({
    "event": "attempt_result",
    "segment_id": seg_id,
    "completed": True,
    "time_ms": 4500,
})

# After:
session.receive_result(AttemptResultEvent(
    segment_id=seg_id,
    completed=True,
    time_ms=4500,
))
```

Note: the `"event": "attempt_result"` key is dropped — it was only needed for dict-based dispatch.

- [ ] **Step 5: Update test_practice_coverage.py**

Add import:

```python
from spinlab.protocol import AttemptResultEvent
```

Replace all 3 `receive_result({...})` dict calls with `receive_result(AttemptResultEvent(...))`, same pattern as Step 4.

- [ ] **Step 6: Run tests to verify**

Run: `python -m pytest tests/unit/test_practice.py tests/unit/test_practice_coverage.py -v`

Expected: All tests pass.

- [ ] **Step 7: Commit**

```
feat: type PracticeSession.receive_result to accept AttemptResultEvent
```

---

### Task 5: Type `SpeedRunSession` — split `receive_event` into 3 methods

**Files:**
- Modify: `python/spinlab/speed_run.py`
- Modify: `tests/unit/test_speed_run_mode.py`

- [ ] **Step 1: Add type alias and imports to speed_run.py**

Add imports:

```python
from .protocol import (
    SpeedRunCheckpointEvent,
    SpeedRunCompleteEvent,
    SpeedRunDeathEvent,
    SpeedRunLoadCmd,
    SpeedRunStopCmd,
)
```

Remove `SpeedRunLoadCmd` and `SpeedRunStopCmd` from the existing import (consolidating).

Add the type alias after imports:

```python
SpeedRunEvent = SpeedRunCheckpointEvent | SpeedRunDeathEvent | SpeedRunCompleteEvent
```

- [ ] **Step 2: Change `_event_queue` type and split `receive_event`**

Change `self._event_queue: asyncio.Queue = asyncio.Queue()` to `self._event_queue: asyncio.Queue[SpeedRunEvent] = asyncio.Queue()`.

Replace `receive_event` with three methods:

```python
def receive_checkpoint(self, event: SpeedRunCheckpointEvent) -> None:
    """Called by SessionManager when a speed_run_checkpoint event arrives."""
    self._event_queue.put_nowait(event)

def receive_death(self, event: SpeedRunDeathEvent) -> None:
    """Called by SessionManager when a speed_run_death event arrives."""
    self._event_queue.put_nowait(event)

def receive_complete(self, event: SpeedRunCompleteEvent) -> None:
    """Called by SessionManager when a speed_run_complete event arrives."""
    self._event_queue.put_nowait(event)
```

- [ ] **Step 3: Update `run_one` dispatch to use `isinstance`**

Replace the string-based dispatch block in `run_one`:

```python
# Before:
event_type = event.get("event")

if event_type == "speed_run_checkpoint":
    if cold_since and current_sub_index < len(level.segments):
        self._record_attempt(
            level.segments[current_sub_index],
            time_ms=event.get("split_ms", 0),
            completed=True,
        )
    current_sub_index += 1
    cold_since = False

elif event_type == "speed_run_death":
    cold_since = True

elif event_type == "speed_run_complete":
    if cold_since and current_sub_index < len(level.segments):
        self._record_attempt(
            level.segments[current_sub_index],
            time_ms=event.get("split_ms", 0),
            completed=True,
        )
    self.levels_completed += 1
    self.current_level_index += 1
    break
```

```python
# After:
if isinstance(event, SpeedRunCheckpointEvent):
    if cold_since and current_sub_index < len(level.segments):
        self._record_attempt(
            level.segments[current_sub_index],
            time_ms=event.split_ms,
            completed=True,
        )
    current_sub_index += 1
    cold_since = False

elif isinstance(event, SpeedRunDeathEvent):
    cold_since = True

elif isinstance(event, SpeedRunCompleteEvent):
    if cold_since and current_sub_index < len(level.segments):
        self._record_attempt(
            level.segments[current_sub_index],
            time_ms=event.split_ms,
            completed=True,
        )
    self.levels_completed += 1
    self.current_level_index += 1
    break
```

- [ ] **Step 4: Update test_speed_run_mode.py**

Replace all `sr.receive_event({...})` calls with the corresponding typed method. There are 8 `receive_event` calls in this file.

Import the event types (they're already imported for the parse tests, just need to use them):

`sr.receive_event({"event": "speed_run_complete", ...})` becomes `sr.receive_complete(SpeedRunCompleteEvent(...))`.

`sr.receive_event({"event": "speed_run_checkpoint", ...})` becomes `sr.receive_checkpoint(SpeedRunCheckpointEvent(...))`.

`sr.receive_event({"event": "speed_run_death", ...})` becomes `sr.receive_death(SpeedRunDeathEvent(...))`.

Example:

```python
# Before:
sr.receive_event({
    "event": "speed_run_complete",
    "elapsed_ms": 30000,
    "split_ms": 30000,
})

# After:
sr.receive_complete(SpeedRunCompleteEvent(
    elapsed_ms=30000,
    split_ms=30000,
))
```

- [ ] **Step 5: Run tests to verify**

Run: `python -m pytest tests/unit/test_speed_run_mode.py -v`

Expected: All tests pass.

- [ ] **Step 6: Commit**

```
feat: split SpeedRunSession.receive_event into typed receive_checkpoint/death/complete
```

---

### Task 6: Remove `dataclasses.asdict()` from SessionManager

All downstream consumers now accept typed events. Wire the final layer.

**Files:**
- Modify: `python/spinlab/session_manager.py`
- Modify: `tests/unit/capture/test_draft.py`

- [ ] **Step 1: Update `_handle_level_entrance`**

```python
async def _handle_level_entrance(self, event: LevelEntranceEvent) -> None:
    if self.mode not in (Mode.REFERENCE, Mode.REPLAY):
        return
    self.capture.handle_entrance(event)
    await self._notify_sse()
```

- [ ] **Step 2: Update `_handle_checkpoint`**

```python
async def _handle_checkpoint(self, event: CheckpointEvent) -> None:
    if self.mode not in (Mode.REFERENCE, Mode.REPLAY):
        return
    self.capture.handle_checkpoint(event, self.require_game())
    await self._notify_sse()
```

- [ ] **Step 3: Update `_handle_death`**

```python
async def _handle_death(self, event: DeathEvent) -> None:
    if self.mode not in (Mode.REFERENCE, Mode.REPLAY, Mode.COLD_FILL):
        return
    if self.mode == Mode.COLD_FILL:
        logger.info("death during cold_fill — waiting for respawn")
    if self.mode in (Mode.REFERENCE, Mode.REPLAY):
        self.capture.handle_death(event)
```

- [ ] **Step 4: Update `_handle_spawn`**

```python
async def _handle_spawn(self, event: SpawnEvent) -> None:
    if self.mode == Mode.COLD_FILL:
        done = await self.cold_fill.handle_spawn(event)
        if done:
            self.mode = Mode.IDLE
        await self._notify_sse()
        return
    if self.mode == Mode.FILL_GAP:
        if self.capture.handle_fill_gap_spawn(dataclasses.asdict(event)):
            self.mode = Mode.IDLE
            await self._notify_sse()
        return
    if self.mode in (Mode.REFERENCE, Mode.REPLAY):
        self.capture.handle_spawn(event, self.require_game())
```

Note: `handle_fill_gap_spawn` still takes a dict — it's in `ReferenceController` and accesses `event.get("state_captured")` and `event["state_path"]`. Since this method is small and only called here, update it inline now too.

- [ ] **Step 5: Update `handle_fill_gap_spawn` in reference.py**

Change `handle_fill_gap_spawn(self, event: dict)` to `handle_fill_gap_spawn(self, event: SpawnEvent)`:

```python
def handle_fill_gap_spawn(self, event: SpawnEvent) -> bool:
    """Returns True if cold save state was captured and mode should return to IDLE."""
    if not event.state_captured or not self.fill_gap_segment_id:
        return False
    waypoint_id = self._fill_gap_waypoint_id
    if waypoint_id:
        from ..models import WaypointSaveState
        self.db.add_save_state(WaypointSaveState(
            waypoint_id=waypoint_id,
            variant_type="cold",
            state_path=event.state_path,
            is_default=True,
        ))
    self.fill_gap_segment_id = None
    self._fill_gap_waypoint_id = None
    return True
```

Now remove the `dataclasses.asdict(event)` from `_handle_spawn` and pass `event` directly:

```python
async def _handle_spawn(self, event: SpawnEvent) -> None:
    if self.mode == Mode.COLD_FILL:
        done = await self.cold_fill.handle_spawn(event)
        if done:
            self.mode = Mode.IDLE
        await self._notify_sse()
        return
    if self.mode == Mode.FILL_GAP:
        if self.capture.handle_fill_gap_spawn(event):
            self.mode = Mode.IDLE
            await self._notify_sse()
        return
    if self.mode in (Mode.REFERENCE, Mode.REPLAY):
        self.capture.handle_spawn(event, self.require_game())
```

- [ ] **Step 6: Update `_handle_level_exit`**

```python
async def _handle_level_exit(self, event: LevelExitEvent) -> None:
    if self.mode not in (Mode.REFERENCE, Mode.REPLAY):
        return
    self.capture.handle_exit(event, self.require_game())
    await self._notify_sse()
```

- [ ] **Step 7: Update `_handle_attempt_result`**

```python
async def _handle_attempt_result(self, event: AttemptResultEvent) -> None:
    if self.mode != Mode.PRACTICE:
        return
    if self.practice_session:
        self.practice_session.receive_result(event)
    await self._notify_sse()
```

- [ ] **Step 8: Update `_handle_rec_saved`**

```python
async def _handle_rec_saved(self, event: RecSavedEvent) -> None:
    self.capture.handle_rec_saved(event)
```

- [ ] **Step 9: Update speed run handlers**

```python
async def _handle_speed_run_checkpoint(self, event: SpeedRunCheckpointEvent) -> None:
    if self.mode != Mode.SPEED_RUN or not self.speed_run_session:
        return
    self.speed_run_session.receive_checkpoint(event)
    await self._notify_sse()

async def _handle_speed_run_death(self, event: SpeedRunDeathEvent) -> None:
    if self.mode != Mode.SPEED_RUN or not self.speed_run_session:
        return
    self.speed_run_session.receive_death(event)
    await self._notify_sse()

async def _handle_speed_run_complete(self, event: SpeedRunCompleteEvent) -> None:
    if self.mode != Mode.SPEED_RUN or not self.speed_run_session:
        return
    self.speed_run_session.receive_complete(event)
    await self._notify_sse()
```

- [ ] **Step 10: Clean up imports in session_manager.py**

Remove `dataclasses` from the import if no longer used. Check: `SegmentCommand.to_dict()` uses `dataclasses.asdict` but that's in `models.py`, not here. The `dataclasses.asdict` import in session_manager was only used for event conversion, so it can be removed.

Keep the `import dataclasses` only if still needed (it's currently used in `dataclasses.asdict` calls which are all being removed). Remove it.

- [ ] **Step 11: Update test_draft.py seeding regression test**

The `test_save_draft_seeds_attempts_and_rebuilds_model` test at line 308 currently uses `asdict(LevelEntranceEvent(...))` and `asdict(LevelExitEvent(...))`. Update to pass typed events directly:

```python
# Before:
from dataclasses import asdict
# ...
entrance = asdict(LevelEntranceEvent(level=1, timestamp_ms=0))
recorder.handle_entrance(entrance)
exit_event = asdict(LevelExitEvent(level=1, goal="exit", timestamp_ms=12345))
recorder.handle_exit(exit_event, game_id="g", db=db, registry=registry)

# After (remove the asdict import):
recorder.handle_entrance(LevelEntranceEvent(level=1, timestamp_ms=0))
recorder.handle_exit(LevelExitEvent(level=1, goal="exit", timestamp_ms=12345),
                     game_id="g", db=db, registry=registry)
```

Update the test's docstring to remove the mention of `asdict` since the point of the test (verifying that protocol events carry the fields the recorder needs) is now self-evident from the typed call.

- [ ] **Step 12: Run full test suite**

Run: `python -m pytest`

Expected: All tests pass. Zero `dataclasses.asdict` calls remain on any event path.

- [ ] **Step 13: Commit**

```
feat: remove all dataclasses.asdict() from event pipeline

SessionManager now passes typed protocol events directly to all
downstream consumers. No dict round-trip remains on any event path.
```
