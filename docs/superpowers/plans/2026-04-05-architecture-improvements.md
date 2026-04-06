# Architecture Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Clean up structural boundaries in the Python backend — unified system state, cold-fill extraction, typed TCP protocol, and SessionManager boilerplate reduction.

**Architecture:** Four sequential refactors: (1) extract SystemState dataclass as single source of truth for mode + sub-states, (2) extract ColdFillController from CaptureController, (3) define typed TCP protocol dataclasses replacing stringly-typed messages, (4) add `_apply_result()` helper to SessionManager. Each section leaves all tests green before moving on.

**Tech Stack:** Python 3.11+, dataclasses, pytest, asyncio, Lua (Mesen2 TCP changes)

**Spec:** `docs/superpowers/specs/2026-04-05-architecture-improvements-design.md`

---

### Task 1: Extract SystemState dataclass

**Files:**
- Create: `python/spinlab/system_state.py`
- Modify: `python/spinlab/models.py` (import only — no model changes)
- Test: `tests/test_system_state.py`

This task creates the `SystemState` dataclass and its sub-state types. It does NOT wire it into SessionManager yet — that's Task 2.

- [ ] **Step 1: Write tests for SystemState and sub-state dataclasses**

```python
# tests/test_system_state.py
"""Tests for SystemState — single source of truth for system mode and sub-states."""
from spinlab.models import Mode
from spinlab.system_state import (
    CaptureState, ColdFillState, DraftState, FillGapState,
    PracticeState, SystemState,
)


class TestSystemStateDefaults:
    def test_defaults_to_idle_with_no_substates(self):
        state = SystemState()
        assert state.mode == Mode.IDLE
        assert state.game_id is None
        assert state.game_name is None
        assert state.capture is None
        assert state.draft is None
        assert state.cold_fill is None
        assert state.fill_gap is None
        assert state.practice is None


class TestSubStates:
    def test_capture_state(self):
        cs = CaptureState(run_id="run_abc")
        assert cs.run_id == "run_abc"
        assert cs.rec_path is None
        assert cs.segments_count == 0

    def test_draft_state(self):
        ds = DraftState(run_id="run_abc", segment_count=3)
        assert ds.run_id == "run_abc"
        assert ds.segment_count == 3

    def test_cold_fill_state(self):
        cfs = ColdFillState(
            current_segment_id="seg1", current_num=1,
            total=3, segment_label="L105 cp1 > cp2",
        )
        assert cfs.current_segment_id == "seg1"
        assert cfs.total == 3

    def test_fill_gap_state(self):
        fgs = FillGapState(segment_id="seg1", waypoint_id="wp1")
        assert fgs.segment_id == "seg1"

    def test_practice_state(self):
        ps = PracticeState(session_id="sess1", started_at="2026-01-01T00:00:00")
        assert ps.session_id == "sess1"
        assert ps.current_segment_id is None
        assert ps.segments_attempted == 0
        assert ps.segments_completed == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_system_state.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'spinlab.system_state'`

- [ ] **Step 3: Implement SystemState**

```python
# python/spinlab/system_state.py
"""SystemState — single source of truth for what the system is doing right now."""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import Mode


@dataclass
class CaptureState:
    """Active reference or replay capture."""
    run_id: str
    rec_path: str | None = None
    segments_count: int = 0


@dataclass
class DraftState:
    """Pending draft after capture (waiting for save/discard)."""
    run_id: str
    segment_count: int


@dataclass
class ColdFillState:
    """Cold-fill queue progress."""
    current_segment_id: str
    current_num: int
    total: int
    segment_label: str


@dataclass
class FillGapState:
    """Fill-gap for a single segment."""
    segment_id: str
    waypoint_id: str


@dataclass
class PracticeState:
    """Active practice session."""
    session_id: str
    started_at: str
    current_segment_id: str | None = None
    segments_attempted: int = 0
    segments_completed: int = 0


@dataclass
class SystemState:
    """Single source of truth for the system's current mode and associated sub-state."""
    mode: Mode = Mode.IDLE
    game_id: str | None = None
    game_name: str | None = None
    capture: CaptureState | None = None
    draft: DraftState | None = None
    cold_fill: ColdFillState | None = None
    fill_gap: FillGapState | None = None
    practice: PracticeState | None = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_system_state.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/system_state.py tests/test_system_state.py
git commit -m "feat: add SystemState dataclass as single source of truth for mode + sub-states"
```

---

### Task 2: Wire SystemState into SessionManager

**Files:**
- Modify: `python/spinlab/session_manager.py`
- Modify: `python/spinlab/state_builder.py`
- Test: existing `tests/test_session_manager.py` (must stay green)

This replaces SessionManager's scattered state fields (`self.mode`, `self.game_id`, `self.game_name`) with a single `self.state: SystemState` instance. The backward-compat properties (`ref_capture`, `draft`, `fill_gap_segment_id`) remain for now — they'll be cleaned up in later tasks as consumers are updated.

- [ ] **Step 1: Update SessionManager to use SystemState**

In `python/spinlab/session_manager.py`:

Add import at the top:
```python
from .system_state import SystemState
```

Replace the "Core state" block in `__init__` (lines 48-53):
```python
        # Core state
        self.mode: Mode = Mode.IDLE
        self.game_id: str | None = None
        self.game_name: str | None = None
        self.scheduler = None  # Scheduler | None, lazy-init
        self.practice_session = None  # PracticeSession | None
        self.practice_task: asyncio.Task | None = None
```

With:
```python
        # Core state — SystemState is the single source of truth
        self.state = SystemState()
        self.scheduler = None  # Scheduler | None, lazy-init
        self.practice_session = None  # PracticeSession | None
        self.practice_task: asyncio.Task | None = None
```

Add property accessors for `mode`, `game_id`, `game_name` so existing code keeps working:
```python
    @property
    def mode(self) -> Mode:
        return self.state.mode

    @mode.setter
    def mode(self, value: Mode) -> None:
        self.state.mode = value

    @property
    def game_id(self) -> str | None:
        return self.state.game_id

    @game_id.setter
    def game_id(self, value: str | None) -> None:
        self.state.game_id = value

    @property
    def game_name(self) -> str | None:
        return self.state.game_name

    @game_name.setter
    def game_name(self, value: str | None) -> None:
        self.state.game_name = value
```

- [ ] **Step 2: Update StateBuilder to read from SystemState**

In `python/spinlab/state_builder.py`, update the `build()` method to read from `session.state` where it currently reads individual fields. The key change is replacing `session.capture.ref_capture.capture_run_id` with `session.state.capture.run_id` when a CaptureState exists — but since CaptureState isn't populated yet (that comes in Tasks 3-4), keep the existing reads for now. This step is just making sure SessionManager's state properties work.

No changes needed yet — the property accessors make this transparent.

- [ ] **Step 3: Run the full test suite to verify nothing broke**

Run: `pytest -m "not (emulator or slow)" -v`
Expected: all PASS — property accessors are transparent to all existing code

- [ ] **Step 4: Commit**

```bash
git add python/spinlab/session_manager.py
git commit -m "refactor: replace scattered state fields with SystemState on SessionManager"
```

---

### Task 3: Extract ColdFillController

**Files:**
- Create: `python/spinlab/cold_fill_controller.py`
- Modify: `python/spinlab/capture_controller.py` (remove cold-fill methods)
- Modify: `python/spinlab/session_manager.py` (wire new controller)
- Modify: `tests/test_cold_fill.py` (update imports)

- [ ] **Step 1: Create ColdFillController by extracting from CaptureController**

Extract the cold-fill methods and state from `capture_controller.py` lines 192-259 into a new file:

```python
# python/spinlab/cold_fill_controller.py
"""ColdFillController — captures cold save states for segments missing them."""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from .models import ActionResult, Mode, Status, WaypointSaveState

if TYPE_CHECKING:
    from .db import Database
    from .tcp_manager import TcpManager

logger = logging.getLogger(__name__)


class ColdFillController:
    """Manages the cold-fill queue: loads hot states, waits for death+respawn,
    captures the resulting cold save state."""

    def __init__(self, db: "Database", tcp: "TcpManager") -> None:
        self.db = db
        self.tcp = tcp
        self.queue: list[dict] = []
        self.current: str | None = None
        self.cold_waypoint_id: str | None = None
        self.total: int = 0

    async def start(self, game_id: str) -> ActionResult:
        """Begin cold-fill for all segments missing cold save states."""
        if not self.tcp.is_connected:
            return ActionResult(status=Status.NOT_CONNECTED)
        gaps = self.db.segments_missing_cold(game_id)
        if not gaps:
            return ActionResult(status=Status.NO_GAPS)
        self.queue = gaps
        self.total = len(gaps)
        self.current = None
        return await self._load_next()

    async def _load_next(self) -> ActionResult:
        seg = self.queue[0]
        self.current = seg["segment_id"]
        row = self.db.conn.execute(
            "SELECT start_waypoint_id FROM segments WHERE id = ?",
            (seg["segment_id"],),
        ).fetchone()
        self.cold_waypoint_id = row[0] if row else None
        await self.tcp.send(json.dumps({
            "event": "cold_fill_load",
            "state_path": seg["hot_state_path"],
            "segment_id": seg["segment_id"],
        }))
        return ActionResult(status=Status.STARTED, new_mode=Mode.COLD_FILL)

    async def handle_spawn(self, event: dict) -> bool:
        """Store cold save state, advance queue. Returns True when all done."""
        if not event.get("state_captured") or not self.current:
            return False
        if self.cold_waypoint_id:
            self.db.add_save_state(WaypointSaveState(
                waypoint_id=self.cold_waypoint_id,
                variant_type="cold",
                state_path=event["state_path"],
                is_default=True,
            ))
        self.queue.pop(0)
        if not self.queue:
            self.current = None
            self.cold_waypoint_id = None
            return True
        await self._load_next()
        return False

    def clear(self) -> None:
        """Reset cold-fill state (e.g., on disconnect)."""
        self.queue = []
        self.current = None
        self.total = 0

    def get_state(self) -> dict | None:
        """Return cold-fill progress dict for state snapshots, or None."""
        if not self.current:
            return None
        current_num = self.total - len(self.queue) + 1
        seg = self.queue[0] if self.queue else None
        label = ""
        if seg:
            start = "start" if seg["start_type"] == "entrance" else f"cp{seg['start_ordinal']}"
            end = "goal" if seg["end_type"] == "goal" else f"cp{seg['end_ordinal']}"
            label = seg.get("description") or f"L{seg['level_number']} {start} > {end}"
        return {
            "current": current_num,
            "total": self.total,
            "segment_label": label,
        }
```

- [ ] **Step 2: Remove cold-fill methods from CaptureController**

In `python/spinlab/capture_controller.py`, delete:
- `start_cold_fill()` method (lines 192-201)
- `_load_next_cold_fill()` method (lines 203-217)
- `handle_cold_fill_spawn()` method (lines 219-237)
- `clear_cold_fill()` method (lines 239-243)
- `get_cold_fill_state()` method (lines 245-259)
- Cold-fill state from `__init__` (lines 39-42): `self.cold_fill_queue`, `self.cold_fill_current`, `self.cold_fill_cold_waypoint_id`, `self.cold_fill_total`

- [ ] **Step 3: Wire ColdFillController into SessionManager**

In `python/spinlab/session_manager.py`:

Add import:
```python
from .cold_fill_controller import ColdFillController
```

In `__init__`, add after the CaptureController line:
```python
        self.cold_fill = ColdFillController(db, tcp)
```

Update `_handle_spawn` — replace `self.capture.handle_cold_fill_spawn` with `self.cold_fill.handle_spawn`:
```python
    async def _handle_spawn(self, event: dict) -> None:
        if self.mode == Mode.COLD_FILL:
            done = await self.cold_fill.handle_spawn(event)
            if done:
                self.mode = Mode.IDLE
            await self._notify_sse()
            return
```

Update `save_draft` — replace `self.capture.start_cold_fill` with `self.cold_fill.start`:
```python
    async def save_draft(self, name: str) -> ActionResult:
        result = await self.capture.save_draft(name)
        if result.status == Status.OK and self.game_id and self.tcp.is_connected:
            cf_result = await self.cold_fill.start(self.game_id)
            if cf_result.new_mode == Mode.COLD_FILL:
                self.mode = Mode.COLD_FILL
        await self._notify_sse()
        return result
```

Update `on_disconnect` — replace `self.capture.clear_cold_fill()` with `self.cold_fill.clear()`:
```python
    def on_disconnect(self) -> None:
        if self.practice_session and self.practice_session.is_running:
            self.practice_session.is_running = False
        self.cold_fill.clear()
        self.capture.handle_disconnect()
        self._clear_ref_and_idle()
```

Update `get_state` in `state_builder.py` — replace `session.capture.get_cold_fill_state()` with `session.cold_fill.get_state()`:
```python
        if session.mode == Mode.COLD_FILL:
            cf_state = session.cold_fill.get_state()
            if cf_state:
                base["cold_fill"] = cf_state
```

- [ ] **Step 4: Update cold-fill tests to use ColdFillController directly**

In `tests/test_cold_fill.py`, update the import and construction:

Replace:
```python
from spinlab.capture_controller import CaptureController
```
With:
```python
from spinlab.cold_fill_controller import ColdFillController
```

In every test method, replace `cc = CaptureController(db, tcp)` with `cc = ColdFillController(db, tcp)`.

Update method calls:
- `cc.start_cold_fill("g1")` → `cc.start("g1")`
- `cc.handle_cold_fill_spawn(...)` → `cc.handle_spawn(...)`
- `cc.get_cold_fill_state()` → `cc.get_state()`
- `cc.cold_fill_current` → `cc.current`

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_cold_fill.py -v && pytest -m "not (emulator or slow)" -v`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/cold_fill_controller.py python/spinlab/capture_controller.py python/spinlab/session_manager.py python/spinlab/state_builder.py tests/test_cold_fill.py
git commit -m "refactor: extract ColdFillController from CaptureController"
```

---

### Task 4: Add _apply_result() helper to SessionManager

**Files:**
- Modify: `python/spinlab/session_manager.py`
- Test: existing `tests/test_session_manager.py` (must stay green)

- [ ] **Step 1: Add _apply_result() method and refactor action methods**

In `python/spinlab/session_manager.py`, add helper method:

```python
    async def _apply_result(self, result: ActionResult) -> ActionResult:
        """Apply mode transition from result and notify SSE."""
        if result.new_mode is not None:
            self.mode = result.new_mode
        await self._notify_sse()
        return result
```

Then simplify each action method. Replace `start_reference`:
```python
    async def start_reference(self, run_name: str | None = None) -> ActionResult:
        return await self._apply_result(
            await self.capture.start_reference(
                self.mode, self._require_game(), self.data_dir, run_name,
            )
        )
```

Replace `stop_reference`:
```python
    async def stop_reference(self) -> ActionResult:
        return await self._apply_result(
            await self.capture.stop_reference(self.mode)
        )
```

Replace `start_replay`:
```python
    async def start_replay(self, spinrec_path: str, speed: int = 0) -> ActionResult:
        return await self._apply_result(
            await self.capture.start_replay(
                self.mode, self._require_game(), spinrec_path, speed,
            )
        )
```

Replace `stop_replay`:
```python
    async def stop_replay(self) -> ActionResult:
        return await self._apply_result(
            await self.capture.stop_replay(self.mode)
        )
```

Replace `start_fill_gap`:
```python
    async def start_fill_gap(self, segment_id: str) -> ActionResult:
        return await self._apply_result(
            await self.capture.start_fill_gap(segment_id)
        )
```

`save_draft` keeps its custom logic (cold-fill trigger) so it does NOT use `_apply_result`.
`start_practice` and `stop_practice` have custom async lifecycle logic so they also stay as-is.

- [ ] **Step 2: Run tests**

Run: `pytest -m "not (emulator or slow)" -v`
Expected: all PASS — behavior is identical

- [ ] **Step 3: Commit**

```bash
git add python/spinlab/session_manager.py
git commit -m "refactor: add _apply_result() helper, simplify SessionManager action methods"
```

---

### Task 5: Define typed TCP protocol — event dataclasses

**Files:**
- Create: `python/spinlab/protocol.py`
- Test: `tests/test_protocol.py`

This task defines all Lua→Python event dataclasses and the `parse_event()` function. It does NOT change TcpManager or SessionManager yet.

- [ ] **Step 1: Write tests for event parsing**

```python
# tests/test_protocol.py
"""Tests for the typed TCP protocol — message catalog and parsing."""
import pytest

from spinlab.protocol import (
    AttemptInvalidatedEvent,
    AttemptResultEvent,
    CheckpointEvent,
    DeathEvent,
    GameContextEvent,
    LevelEntranceEvent,
    LevelExitEvent,
    RecSavedEvent,
    ReplayErrorEvent,
    ReplayFinishedEvent,
    ReplayProgressEvent,
    ReplayStartedEvent,
    RomInfoEvent,
    SpawnEvent,
    parse_event,
)


class TestParseEvent:
    def test_parse_rom_info(self):
        raw = {"event": "rom_info", "filename": "test.sfc"}
        evt = parse_event(raw)
        assert isinstance(evt, RomInfoEvent)
        assert evt.filename == "test.sfc"

    def test_parse_spawn_with_conditions(self):
        raw = {
            "event": "spawn",
            "level_num": 105,
            "state_captured": True,
            "state_path": "/cold.mss",
            "conditions": {"powerup": 2},
            "is_cold_cp": True,
            "cp_ordinal": 1,
        }
        evt = parse_event(raw)
        assert isinstance(evt, SpawnEvent)
        assert evt.level_num == 105
        assert evt.state_captured is True
        assert evt.conditions == {"powerup": 2}

    def test_parse_death(self):
        evt = parse_event({"event": "death"})
        assert isinstance(evt, DeathEvent)

    def test_parse_attempt_result(self):
        raw = {
            "event": "attempt_result",
            "segment_id": "seg1",
            "completed": True,
            "time_ms": 5000,
            "deaths": 0,
            "clean_tail_ms": 5000,
        }
        evt = parse_event(raw)
        assert isinstance(evt, AttemptResultEvent)
        assert evt.segment_id == "seg1"
        assert evt.completed is True
        assert evt.time_ms == 5000

    def test_parse_level_exit(self):
        raw = {"event": "level_exit", "level": 105, "goal": "normal"}
        evt = parse_event(raw)
        assert isinstance(evt, LevelExitEvent)
        assert evt.level == 105
        assert evt.goal == "normal"

    def test_unknown_event_raises(self):
        with pytest.raises(ValueError, match="Unknown event type"):
            parse_event({"event": "bogus_event"})

    def test_missing_event_field_raises(self):
        with pytest.raises(ValueError, match="Missing 'event' field"):
            parse_event({"not_event": "foo"})

    def test_extra_fields_ignored(self):
        raw = {"event": "death", "unexpected_field": 42}
        evt = parse_event(raw)
        assert isinstance(evt, DeathEvent)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_protocol.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'spinlab.protocol'`

- [ ] **Step 3: Implement protocol.py with event dataclasses and parse_event**

```python
# python/spinlab/protocol.py
"""Typed TCP protocol — message catalog for Lua <-> Python communication.

Every Lua->Python event and Python->Lua command is a dataclass here.
This file is the single source of truth for the IPC contract.
"""
from __future__ import annotations

from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Lua -> Python events
# ---------------------------------------------------------------------------

@dataclass
class RomInfoEvent:
    event: str = "rom_info"
    filename: str = ""

@dataclass
class GameContextEvent:
    event: str = "game_context"
    game_id: str = ""
    game_name: str = ""

@dataclass
class LevelEntranceEvent:
    event: str = "level_entrance"
    level: int = 0
    state_path: str | None = None
    conditions: dict = field(default_factory=dict)

@dataclass
class CheckpointEvent:
    event: str = "checkpoint"
    level_num: int = 0
    cp_ordinal: int = 1
    state_path: str | None = None
    timestamp_ms: int = 0
    conditions: dict = field(default_factory=dict)

@dataclass
class DeathEvent:
    event: str = "death"

@dataclass
class SpawnEvent:
    event: str = "spawn"
    level_num: int = 0
    state_captured: bool = False
    state_path: str | None = None
    conditions: dict = field(default_factory=dict)
    is_cold_cp: bool = False
    cp_ordinal: int | None = None

@dataclass
class LevelExitEvent:
    event: str = "level_exit"
    level: int = 0
    goal: str = "abort"
    conditions: dict = field(default_factory=dict)

@dataclass
class AttemptResultEvent:
    event: str = "attempt_result"
    segment_id: str = ""
    completed: bool = False
    time_ms: int | None = None
    deaths: int = 0
    clean_tail_ms: int | None = None

@dataclass
class RecSavedEvent:
    event: str = "rec_saved"
    path: str = ""
    frame_count: int = 0

@dataclass
class ReplayStartedEvent:
    event: str = "replay_started"
    path: str = ""
    frame_count: int = 0

@dataclass
class ReplayProgressEvent:
    event: str = "replay_progress"
    frame: int = 0
    total: int = 0

@dataclass
class ReplayFinishedEvent:
    event: str = "replay_finished"

@dataclass
class ReplayErrorEvent:
    event: str = "replay_error"
    message: str = ""

@dataclass
class AttemptInvalidatedEvent:
    event: str = "attempt_invalidated"


# ---------------------------------------------------------------------------
# Python -> Lua commands
# ---------------------------------------------------------------------------

@dataclass
class GameContextCmd:
    event: str = "game_context"
    game_id: str = ""
    game_name: str = ""

@dataclass
class ReferenceStartCmd:
    event: str = "reference_start"
    path: str = ""

@dataclass
class ReferenceStopCmd:
    event: str = "reference_stop"

@dataclass
class ReplayCmd:
    event: str = "replay"
    path: str = ""
    speed: int = 0

@dataclass
class ReplayStopCmd:
    event: str = "replay_stop"

@dataclass
class FillGapLoadCmd:
    event: str = "fill_gap_load"
    state_path: str = ""
    message: str = ""

@dataclass
class ColdFillLoadCmd:
    event: str = "cold_fill_load"
    state_path: str = ""
    segment_id: str = ""

@dataclass
class SetConditionsCmd:
    event: str = "set_conditions"
    definitions: list[dict] = field(default_factory=list)

@dataclass
class SetInvalidateComboCmd:
    event: str = "set_invalidate_combo"
    combo: list[str] = field(default_factory=list)

@dataclass
class PracticeLoadCmd:
    event: str = "practice_load"
    id: str = ""
    state_path: str = ""
    description: str = ""
    end_type: str = ""
    expected_time_ms: int | None = None
    auto_advance_delay_ms: int = 1000

@dataclass
class PracticeStopCmd:
    event: str = "practice_stop"


# ---------------------------------------------------------------------------
# Event registry and parser
# ---------------------------------------------------------------------------

# Map event name -> dataclass type for Lua->Python events
_EVENT_REGISTRY: dict[str, type] = {
    "rom_info": RomInfoEvent,
    "game_context": GameContextEvent,
    "level_entrance": LevelEntranceEvent,
    "checkpoint": CheckpointEvent,
    "death": DeathEvent,
    "spawn": SpawnEvent,
    "level_exit": LevelExitEvent,
    "attempt_result": AttemptResultEvent,
    "rec_saved": RecSavedEvent,
    "replay_started": ReplayStartedEvent,
    "replay_progress": ReplayProgressEvent,
    "replay_finished": ReplayFinishedEvent,
    "replay_error": ReplayErrorEvent,
    "attempt_invalidated": AttemptInvalidatedEvent,
}


def parse_event(raw: dict) -> object:
    """Parse a raw JSON dict from Lua into a typed event dataclass.

    Raises ValueError for unknown or malformed events.
    """
    event_name = raw.get("event")
    if event_name is None:
        raise ValueError("Missing 'event' field in TCP message")
    cls = _EVENT_REGISTRY.get(event_name)
    if cls is None:
        raise ValueError(f"Unknown event type: {event_name!r}")
    # Build dataclass from matching keys only, ignoring extras
    import dataclasses
    valid_fields = {f.name for f in dataclasses.fields(cls)}
    kwargs = {k: v for k, v in raw.items() if k in valid_fields}
    return cls(**kwargs)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_protocol.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/protocol.py tests/test_protocol.py
git commit -m "feat: add typed TCP protocol — event dataclasses and parse_event"
```

---

### Task 6: Define typed TCP protocol — command serialization

**Files:**
- Modify: `python/spinlab/protocol.py`
- Modify: `tests/test_protocol.py`

Add `serialize_command()` function for Python→Lua commands.

- [ ] **Step 1: Write tests for command serialization**

Append to `tests/test_protocol.py`:

```python
from spinlab.protocol import (
    ReferenceStartCmd,
    ReferenceStopCmd,
    ReplayCmd,
    SetConditionsCmd,
    SetInvalidateComboCmd,
    PracticeLoadCmd,
    PracticeStopCmd,
    ColdFillLoadCmd,
    FillGapLoadCmd,
    GameContextCmd,
    serialize_command,
)
import json


class TestSerializeCommand:
    def test_reference_start(self):
        cmd = ReferenceStartCmd(path="/rec/run.spinrec")
        msg = serialize_command(cmd)
        parsed = json.loads(msg)
        assert parsed["event"] == "reference_start"
        assert parsed["path"] == "/rec/run.spinrec"

    def test_reference_stop(self):
        msg = serialize_command(ReferenceStopCmd())
        parsed = json.loads(msg)
        assert parsed["event"] == "reference_stop"

    def test_set_conditions(self):
        cmd = SetConditionsCmd(definitions=[
            {"name": "powerup", "address": 25, "size": 1},
        ])
        msg = serialize_command(cmd)
        parsed = json.loads(msg)
        assert parsed["event"] == "set_conditions"
        assert len(parsed["definitions"]) == 1

    def test_practice_load(self):
        cmd = PracticeLoadCmd(
            id="seg1", state_path="/state.mss",
            description="L105 start > goal", end_type="goal",
            expected_time_ms=5000, auto_advance_delay_ms=1000,
        )
        msg = serialize_command(cmd)
        parsed = json.loads(msg)
        assert parsed["event"] == "practice_load"
        assert parsed["id"] == "seg1"
        assert parsed["state_path"] == "/state.mss"

    def test_practice_stop(self):
        msg = serialize_command(PracticeStopCmd())
        parsed = json.loads(msg)
        assert parsed["event"] == "practice_stop"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_protocol.py::TestSerializeCommand -v`
Expected: FAIL — `ImportError: cannot import name 'serialize_command'`

- [ ] **Step 3: Implement serialize_command**

Add to `python/spinlab/protocol.py`:

```python
def serialize_command(cmd) -> str:
    """Serialize a command dataclass to JSON string for sending over TCP."""
    import dataclasses
    return json.dumps(dataclasses.asdict(cmd))
```

And add `import json` at the top of the file if not already present.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_protocol.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/protocol.py tests/test_protocol.py
git commit -m "feat: add serialize_command for typed Python->Lua commands"
```

---

### Task 7: Wire typed protocol into TcpManager

**Files:**
- Modify: `python/spinlab/tcp_manager.py`
- Test: existing tests must stay green

Add a `send_command()` method that accepts protocol dataclasses, and a `parse_event` integration in the read loop.

- [ ] **Step 1: Add send_command() to TcpManager**

In `python/spinlab/tcp_manager.py`, add import:
```python
from .protocol import serialize_command
```

Add method after `send()`:
```python
    async def send_command(self, cmd) -> None:
        """Send a typed protocol command (serialized to JSON)."""
        await self.send(serialize_command(cmd))
```

- [ ] **Step 2: Run tests to verify nothing broke**

Run: `pytest -m "not (emulator or slow)" -v`
Expected: all PASS

- [ ] **Step 3: Commit**

```bash
git add python/spinlab/tcp_manager.py
git commit -m "feat: add TcpManager.send_command() for typed protocol messages"
```

---

### Task 8: Migrate senders to typed commands — CaptureController

**Files:**
- Modify: `python/spinlab/capture_controller.py`
- Test: existing tests must stay green

Replace all `json.dumps({"event": ...})` calls with typed command dataclasses.

- [ ] **Step 1: Update CaptureController sends**

In `python/spinlab/capture_controller.py`, add import:
```python
from .protocol import (
    ReferenceStartCmd, ReferenceStopCmd, ReplayCmd, ReplayStopCmd,
    FillGapLoadCmd,
)
```

Replace in `start_reference` (line 98):
```python
        await self.tcp.send(json.dumps({"event": "reference_start", "path": rec_path}))
```
With:
```python
        await self.tcp.send_command(ReferenceStartCmd(path=rec_path))
```

Replace in `stop_reference` (line 105):
```python
            await self.tcp.send(json.dumps({"event": "reference_stop"}))
```
With:
```python
            await self.tcp.send_command(ReferenceStopCmd())
```

Replace in `start_replay` (line 132):
```python
        await self.tcp.send(json.dumps({"event": "replay", "path": spinrec_path, "speed": speed}))
```
With:
```python
        await self.tcp.send_command(ReplayCmd(path=spinrec_path, speed=speed))
```

Replace in `stop_replay` (line 139):
```python
            await self.tcp.send(json.dumps({"event": "replay_stop"}))
```
With:
```python
            await self.tcp.send_command(ReplayStopCmd())
```

Replace in `start_fill_gap` (lines 166-170):
```python
        await self.tcp.send(json.dumps({
            "event": "fill_gap_load",
            "state_path": hot.state_path,
            "message": "Die to capture cold start",
        }))
```
With:
```python
        await self.tcp.send_command(FillGapLoadCmd(
            state_path=hot.state_path,
            message="Die to capture cold start",
        ))
```

Remove `import json` from the file if no longer needed (check if `json` is used elsewhere — `DraftManager` and other methods may still use it). The `json` import can likely be removed since the remaining code doesn't use `json.dumps` directly, but check first.

- [ ] **Step 2: Run tests**

Run: `pytest -m "not (emulator or slow)" -v`
Expected: all PASS — `send_command` calls `send()` internally, so mock assertions on `tcp.send` still work (the mock will receive serialized JSON strings).

Note: Some tests assert on `tcp.send.call_args` by parsing the JSON. These will still work since `send_command` calls `send` with a JSON string. But if tests assert on `tcp.send_command`, they'll need updating. Check test output carefully.

- [ ] **Step 3: Commit**

```bash
git add python/spinlab/capture_controller.py
git commit -m "refactor: migrate CaptureController TCP sends to typed protocol commands"
```

---

### Task 9: Migrate senders to typed commands — SessionManager and ColdFillController

**Files:**
- Modify: `python/spinlab/session_manager.py`
- Modify: `python/spinlab/cold_fill_controller.py`
- Test: existing tests must stay green

- [ ] **Step 1: Update SessionManager sends**

In `python/spinlab/session_manager.py`, add import:
```python
from .protocol import GameContextCmd, SetConditionsCmd, SetInvalidateComboCmd
```

Replace in `_handle_rom_info` (lines 176-180):
```python
        await self.tcp.send(json.dumps({
            "event": "game_context",
            "game_id": checksum,
            "game_name": name,
        }))
```
With:
```python
        await self.tcp.send_command(GameContextCmd(game_id=checksum, game_name=name))
```

Replace in `_install_condition_registry` (line 193):
```python
                await self.tcp.send(f"set_conditions:{json.dumps(defs_payload)}")
```
With:
```python
                await self.tcp.send_command(SetConditionsCmd(definitions=defs_payload))
```

Replace (line 194):
```python
            await self.tcp.send(f"set_invalidate_combo:{json.dumps(self.invalidate_combo)}")
```
With:
```python
            await self.tcp.send_command(SetInvalidateComboCmd(combo=self.invalidate_combo))
```

Remove `import json` from session_manager.py if no longer needed.

- [ ] **Step 2: Update ColdFillController sends**

In `python/spinlab/cold_fill_controller.py`, add import:
```python
from .protocol import ColdFillLoadCmd
```

Replace in `_load_next` (lines with `json.dumps`):
```python
        await self.tcp.send(json.dumps({
            "event": "cold_fill_load",
            "state_path": seg["hot_state_path"],
            "segment_id": seg["segment_id"],
        }))
```
With:
```python
        await self.tcp.send_command(ColdFillLoadCmd(
            state_path=seg["hot_state_path"],
            segment_id=seg["segment_id"],
        ))
```

Remove `import json` from the file.

- [ ] **Step 3: Update Practice session sends**

In `python/spinlab/practice.py`, add import:
```python
from .protocol import PracticeLoadCmd, PracticeStopCmd
```

Replace in `run_one` (line 143):
```python
        await self.tcp.send("practice_load:" + json.dumps(cmd.to_dict()))
```
With:
```python
        await self.tcp.send_command(PracticeLoadCmd(
            id=cmd.id,
            state_path=cmd.state_path,
            description=cmd.description,
            end_type=cmd.end_type,
            expected_time_ms=cmd.expected_time_ms,
            auto_advance_delay_ms=cmd.auto_advance_delay_ms,
        ))
```

Replace in `run_loop` (line 203):
```python
                await self.tcp.send("practice_stop")
```
With:
```python
                await self.tcp.send_command(PracticeStopCmd())
```

- [ ] **Step 4: Run tests**

Run: `pytest -m "not (emulator or slow)" -v`
Expected: all PASS. If any tests assert specific TCP send payloads, they may need updating since `send_command` calls `send()` with JSON (not colon-delimited strings).

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/session_manager.py python/spinlab/cold_fill_controller.py python/spinlab/practice.py
git commit -m "refactor: migrate all TCP sends to typed protocol commands"
```

---

### Task 10: Wire typed event parsing into SessionManager

**Files:**
- Modify: `python/spinlab/session_manager.py`
- Modify: `tests/test_session_manager.py` (if handlers are tested with raw dicts)

Replace `route_event(event: dict)` with typed parsing. Handlers receive typed event objects.

- [ ] **Step 1: Update route_event to parse typed events**

In `python/spinlab/session_manager.py`, add import:
```python
from .protocol import (
    parse_event,
    RomInfoEvent, GameContextEvent, LevelEntranceEvent, CheckpointEvent,
    DeathEvent, SpawnEvent, LevelExitEvent, AttemptResultEvent,
    RecSavedEvent, ReplayStartedEvent, ReplayProgressEvent,
    ReplayFinishedEvent, ReplayErrorEvent, AttemptInvalidatedEvent,
)
```

Replace the `_event_handlers` dict and `route_event` method:

```python
        # Event dispatch table — keyed by event dataclass type
        self._event_handlers: dict[type, callable] = {
            RomInfoEvent: self._handle_rom_info,
            GameContextEvent: self._handle_game_context,
            LevelEntranceEvent: self._handle_level_entrance,
            CheckpointEvent: self._handle_checkpoint,
            DeathEvent: self._handle_death,
            SpawnEvent: self._handle_spawn,
            LevelExitEvent: self._handle_level_exit,
            AttemptResultEvent: self._handle_attempt_result,
            RecSavedEvent: self._handle_rec_saved,
            ReplayStartedEvent: self._handle_replay_started,
            ReplayProgressEvent: self._handle_replay_progress,
            ReplayFinishedEvent: self._handle_replay_finished,
            ReplayErrorEvent: self._handle_replay_error,
            AttemptInvalidatedEvent: self._handle_attempt_invalidated,
        }
```

```python
    async def route_event(self, event: dict) -> None:
        try:
            typed_event = parse_event(event)
        except ValueError:
            logger.warning("Unknown/malformed event from Lua: %r", event)
            return
        handler = self._event_handlers.get(type(typed_event))
        if handler:
            await handler(typed_event)
```

- [ ] **Step 2: Update handler signatures to accept typed events**

Update each handler. For example, `_handle_rom_info`:
```python
    async def _handle_rom_info(self, event: RomInfoEvent) -> None:
        filename = event.filename
        if not self.rom_dir or not filename:
            return
        # ... rest unchanged, but replace event.get("filename", "") with event.filename
```

For `_handle_spawn`:
```python
    async def _handle_spawn(self, event: SpawnEvent) -> None:
        if self.mode == Mode.COLD_FILL:
            done = await self.cold_fill.handle_spawn({"state_captured": event.state_captured, "state_path": event.state_path})
            # ...
```

Note: `ColdFillController.handle_spawn`, `CaptureController.handle_*`, and `ReferenceCapture.handle_*` still accept raw dicts. To avoid changing every downstream signature in this task, convert the typed event back to a dict for delegation:

```python
    import dataclasses

    async def _handle_spawn(self, event: SpawnEvent) -> None:
        event_dict = dataclasses.asdict(event)
        if self.mode == Mode.COLD_FILL:
            done = await self.cold_fill.handle_spawn(event_dict)
            # ...
```

This is a pragmatic bridge — downstream consumers can be migrated to accept typed events in a follow-up.

Similarly for `_handle_level_entrance`, `_handle_checkpoint`, `_handle_level_exit`, `_handle_attempt_result`, etc.

For simple handlers that don't read event fields (like `_handle_death`, `_handle_replay_started`), just update the signature:
```python
    async def _handle_death(self, event: DeathEvent) -> None:
        if self.mode not in (Mode.REFERENCE, Mode.REPLAY, Mode.COLD_FILL):
            return
        if self.mode in (Mode.REFERENCE, Mode.REPLAY):
            self.capture.handle_death()
```

For `_handle_attempt_result`, PracticeSession.receive_result still takes a dict:
```python
    async def _handle_attempt_result(self, event: AttemptResultEvent) -> None:
        if self.mode != Mode.PRACTICE:
            return
        if self.practice_session:
            self.practice_session.receive_result(dataclasses.asdict(event))
        await self._notify_sse()
```

For `_handle_rec_saved`:
```python
    async def _handle_rec_saved(self, event: RecSavedEvent) -> None:
        self.capture.handle_rec_saved({"path": event.path})
```

- [ ] **Step 3: Remove EventType import if no longer used**

Check if `EventType` is still used anywhere in session_manager.py. If not, remove the import.

- [ ] **Step 4: Run tests**

Run: `pytest -m "not (emulator or slow)" -v`

Tests that call `session.route_event({"event": "...", ...})` should still work — `route_event` still accepts a dict and parses it internally.

Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/session_manager.py
git commit -m "refactor: wire typed event parsing into SessionManager.route_event"
```

---

### Task 11: Update Lua TCP dispatch to handle JSON commands

**Files:**
- Modify: `lua/spinlab.lua`

The Python side now sends `set_conditions` and `set_invalidate_combo` as JSON objects instead of `prefix:payload` strings. And `practice_load` and `practice_stop` are now JSON. The Lua dispatcher needs to handle these in `handle_json_message`.

- [ ] **Step 1: Add json_get_arr helper and new JSON command handlers**

The Lua JSON parsing is regex-based (`json_get_str`, `json_get_num`, `json_get_bool`). There's no `json_get_arr` yet. We need one to extract array substrings from JSON objects.

Add this helper near the other `json_get_*` functions (after line 294):

```lua
-- Extract a JSON array field as a raw substring (for passing to parse_conditions_json / parse_string_array).
-- Returns nil if the key is not found.
local function json_get_arr(json_str, key)
  -- Match "key": [ ... ] — find the opening bracket, then balance brackets to find the close
  local start = json_str:find('"' .. key .. '"%s*:%s*%[')
  if not start then return nil end
  local arr_start = json_str:find('%[', start)
  if not arr_start then return nil end
  local depth = 0
  for i = arr_start, #json_str do
    local c = json_str:sub(i, i)
    if c == '[' then depth = depth + 1
    elseif c == ']' then
      depth = depth - 1
      if depth == 0 then
        return json_str:sub(arr_start, i)
      end
    end
  end
  return nil
end
```

Then in `handle_json_message` (line 931), after the `elseif decoded_event == "cold_fill_load"` block, add:

```lua
  elseif decoded_event == "set_conditions" then
    local defs_str = json_get_arr(line, "definitions")
    if not defs_str then
      client:send("err:set_conditions_invalid\n")
      return
    end
    local defs, err = parse_conditions_json(defs_str)
    if not defs then
      log("set_conditions: invalid payload — " .. tostring(err))
      client:send("err:set_conditions_invalid\n")
      return
    end
    condition_defs = defs
    client:send("ok:conditions_set\n")
    log("set_conditions: loaded " .. #condition_defs .. " conditions")
  elseif decoded_event == "set_invalidate_combo" then
    local combo_str = json_get_arr(line, "combo")
    if not combo_str then
      client:send("err:set_invalidate_combo_invalid\n")
      return
    end
    local ok, result = pcall(parse_string_array, combo_str)
    if not ok then
      log("set_invalidate_combo: invalid payload — " .. tostring(result))
      client:send("err:set_invalidate_combo_invalid\n")
      return
    end
    invalidate_combo = result
    client:send("ok:invalidate_combo_set\n")
    log("set_invalidate_combo: " .. table.concat(result, ","))
```

- [ ] **Step 2: Add practice_load and practice_stop to handle_json_message**

Add before the closing `else` of `handle_json_message`:

```lua
  elseif decoded_event == "practice_load" then
    -- New JSON format: full object with all fields
    practice.segment = parse_practice_segment(line)
    practice.auto_advance_ms = practice.segment.auto_advance_delay_ms or 2000
    practice.active = true
    practice.state = PSTATE_LOADING
    local sp = practice.segment.state_path
    if not sp or sp == "" then
      log("ERROR: No valid state_path for segment " .. (practice.segment.id or "?"))
      client:send("err:no_state_path\n")
      practice_reset()
    else
      table.insert(pending_loads, sp)
      practice.start_ms = ts_ms()
      client:send("ok:queued\n")
      log("Practice load queued: " .. (practice.segment.id or "?"))
    end
  elseif decoded_event == "practice_stop" then
    practice_reset()
    client:send("ok:practice_stopped\n")
    log("Practice stopped by command")
```

- [ ] **Step 3: Keep old prefixed handlers for backward compatibility during transition**

The old `prefixed_commands["set_conditions"]`, `prefixed_commands["set_invalidate_combo"]`, `prefixed_commands["practice_load"]` handlers in lines 1079-1120 should remain temporarily. They can be removed in a future cleanup once all Python sends use the new format. Add a comment:

```lua
  -- DEPRECATED: these prefixed handlers are kept for backward compatibility.
  -- Remove once all Python code uses JSON-only commands via send_command().
```

- [ ] **Step 4: Remove `practice_stop` from `text_commands`**

Since `practice_stop` is now a JSON command, add it to `handle_json_message` as shown above. The old `text_commands["practice_stop"]` can be kept for compatibility or removed if all Python code is updated.

- [ ] **Step 5: Test manually with the emulator if possible, or run emulator tests**

Run: `pytest -m emulator -v` (if Mesen2 is available)
Otherwise: verify by reading the Lua code for consistency.

- [ ] **Step 6: Commit**

```bash
git add lua/spinlab.lua
git commit -m "feat(lua): handle set_conditions, set_invalidate_combo, practice_load as JSON commands"
```

---

### Task 12: Run full test suite and verify

**Files:** None (verification only)

- [ ] **Step 1: Run all fast tests**

Run: `pytest -m "not (emulator or slow)" -v`
Expected: all PASS

- [ ] **Step 2: Run slow tests**

Run: `pytest -m slow -v`
Expected: all PASS

- [ ] **Step 3: Run frontend tests**

Run: `cd frontend && npm test`
Expected: all PASS (frontend unchanged)

- [ ] **Step 4: Run full suite**

Run: `pytest`
Expected: all PASS

- [ ] **Step 5: Commit any fixups if needed, then final commit**

```bash
git add -A
git commit -m "chore: architecture improvements complete — SystemState, ColdFillController, typed protocol, _apply_result"
```
