# Speed Run Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a "Speed Run" mode that plays through the game sequentially, level by level, skipping all non-gameplay via save state loading. Checkpoints advance respawn point but don't stop play. Only death (reload at last CP) or goal (advance to next level) end interaction. Cold-start segment attempts are recorded.

**Architecture:** New `Mode.SPEED_RUN` enum value. New `SpeedRunSession` class orchestrates level-by-level play using existing segment/waypoint data. New `speed_run_load` protocol command sends level entrance state + CP save states to Lua. New `handle_speed_run` Lua state machine manages respawn tracking. Python tracks cold/hot state and records only cold attempts.

**Tech Stack:** Python 3.11+ (FastAPI, asyncio), Lua (Mesen2), TypeScript (Vite)

**Spec:** `docs/superpowers/specs/2026-04-07-speed-run-mode-design.md`

---

### Task 1: Mode enum + legal transitions

**Files:**
- Modify: `python/spinlab/models.py:12-28`
- Test: `tests/test_models.py` (or inline — verify transition works)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_speed_run_mode.py
"""Tests for Speed Run mode enum and transitions."""
import pytest
from spinlab.models import Mode, transition_mode


def test_speed_run_mode_exists():
    assert Mode.SPEED_RUN.value == "speed_run"


def test_idle_to_speed_run_legal():
    assert transition_mode(Mode.IDLE, Mode.SPEED_RUN) == Mode.SPEED_RUN


def test_speed_run_to_idle_legal():
    assert transition_mode(Mode.SPEED_RUN, Mode.IDLE) == Mode.IDLE


def test_speed_run_to_practice_illegal():
    with pytest.raises(ValueError):
        transition_mode(Mode.SPEED_RUN, Mode.PRACTICE)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_speed_run_mode.py -v`
Expected: FAIL — `Mode` has no `SPEED_RUN` member

- [ ] **Step 3: Add SPEED_RUN to Mode enum, transitions, AttemptSource, and Status**

In `python/spinlab/models.py`, add to `Mode`:
```python
class Mode(Enum):
    IDLE = "idle"
    REFERENCE = "reference"
    PRACTICE = "practice"
    REPLAY = "replay"
    FILL_GAP = "fill_gap"
    COLD_FILL = "cold_fill"
    SPEED_RUN = "speed_run"
```

Update `_LEGAL_TRANSITIONS`:
```python
_LEGAL_TRANSITIONS: dict[Mode, set[Mode]] = {
    Mode.IDLE: {Mode.REFERENCE, Mode.PRACTICE, Mode.FILL_GAP, Mode.COLD_FILL, Mode.SPEED_RUN},
    Mode.REFERENCE: {Mode.IDLE, Mode.REPLAY},
    Mode.PRACTICE: {Mode.IDLE},
    Mode.REPLAY: {Mode.IDLE},
    Mode.FILL_GAP: {Mode.IDLE},
    Mode.COLD_FILL: {Mode.IDLE},
    Mode.SPEED_RUN: {Mode.IDLE},
}
```

Add to `AttemptSource`:
```python
class AttemptSource(StrEnum):
    PRACTICE = "practice"
    REPLAY = "replay"
    SPEED_RUN = "speed_run"
```

Add to `Status`:
```python
MISSING_SAVE_STATES = "missing_save_states"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_speed_run_mode.py -v`
Expected: PASS

- [ ] **Step 5: Run full fast tests to check for regressions**

Run: `python -m pytest -m "not (emulator or slow or frontend)" -q`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/models.py tests/test_speed_run_mode.py
git commit -m "feat: add SPEED_RUN mode enum and legal transitions"
```

---

### Task 2: Protocol — SpeedRunLoadCmd, SpeedRunStopCmd, and events

**Files:**
- Modify: `python/spinlab/protocol.py`
- Test: `tests/test_speed_run_mode.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_speed_run_mode.py`:

```python
from spinlab.protocol import (
    SpeedRunLoadCmd, SpeedRunStopCmd,
    SpeedRunCheckpointEvent, SpeedRunDeathEvent, SpeedRunCompleteEvent,
    parse_event, serialize_command,
)


def test_speed_run_load_cmd_serializes():
    cmd = SpeedRunLoadCmd(
        id="seg1",
        state_path="/entrance.mss",
        description="Level 1",
        checkpoints=[
            {"ordinal": 1, "state_path": "/cp1.mss"},
            {"ordinal": 2, "state_path": "/cp2.mss"},
        ],
        expected_time_ms=45000,
        auto_advance_delay_ms=1000,
    )
    s = serialize_command(cmd)
    assert '"event": "speed_run_load"' in s or '"event":"speed_run_load"' in s


def test_speed_run_stop_cmd_serializes():
    cmd = SpeedRunStopCmd()
    s = serialize_command(cmd)
    assert "speed_run_stop" in s


def test_parse_speed_run_checkpoint_event():
    raw = {"event": "speed_run_checkpoint", "ordinal": 1, "elapsed_ms": 12340, "split_ms": 12340}
    evt = parse_event(raw)
    assert isinstance(evt, SpeedRunCheckpointEvent)
    assert evt.ordinal == 1
    assert evt.split_ms == 12340


def test_parse_speed_run_death_event():
    raw = {"event": "speed_run_death", "elapsed_ms": 5230, "split_ms": 5230}
    evt = parse_event(raw)
    assert isinstance(evt, SpeedRunDeathEvent)
    assert evt.split_ms == 5230


def test_parse_speed_run_complete_event():
    raw = {"event": "speed_run_complete", "elapsed_ms": 45600, "split_ms": 12000}
    evt = parse_event(raw)
    assert isinstance(evt, SpeedRunCompleteEvent)
    assert evt.elapsed_ms == 45600
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_speed_run_mode.py -v`
Expected: FAIL — imports don't exist

- [ ] **Step 3: Add protocol dataclasses and register events**

In `python/spinlab/protocol.py`, add after the existing event dataclasses (before the commands section):

```python
@dataclass
class SpeedRunCheckpointEvent:
    event: str = "speed_run_checkpoint"
    ordinal: int = 0
    elapsed_ms: int = 0
    split_ms: int = 0

@dataclass
class SpeedRunDeathEvent:
    event: str = "speed_run_death"
    elapsed_ms: int = 0
    split_ms: int = 0

@dataclass
class SpeedRunCompleteEvent:
    event: str = "speed_run_complete"
    elapsed_ms: int = 0
    split_ms: int = 0
```

Add after the existing command dataclasses:

```python
@dataclass
class SpeedRunLoadCmd:
    event: str = "speed_run_load"
    id: str = ""
    state_path: str = ""
    description: str = ""
    checkpoints: list = field(default_factory=list)
    expected_time_ms: int | None = None
    auto_advance_delay_ms: int = 1000

@dataclass
class SpeedRunStopCmd:
    event: str = "speed_run_stop"
```

Add to `_EVENT_REGISTRY`:

```python
"speed_run_checkpoint": SpeedRunCheckpointEvent,
"speed_run_death": SpeedRunDeathEvent,
"speed_run_complete": SpeedRunCompleteEvent,
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_speed_run_mode.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/protocol.py tests/test_speed_run_mode.py
git commit -m "feat: add speed run protocol commands and events"
```

---

### Task 3: SpeedRunSession — level sequencing and cold tracking

**Files:**
- Create: `python/spinlab/speed_run.py`
- Test: `tests/test_speed_run_mode.py`

This is the core orchestrator. It queries segments, groups by level, sends one level at a time, and tracks cold/hot state for recording.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_speed_run_mode.py`:

```python
import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock
from spinlab.db import Database
from spinlab.models import Segment, Waypoint, WaypointSaveState, Attempt
from spinlab.speed_run import SpeedRunSession


def _make_waypoint_and_state(db, game_id, level, ep_type, ordinal, state_path, conditions=None):
    """Create a waypoint + save state, return waypoint."""
    wp = Waypoint.make(game_id, level, ep_type, ordinal, conditions or {})
    db.upsert_waypoint(wp)
    db.add_save_state(WaypointSaveState(
        waypoint_id=wp.id, variant_type="hot",
        state_path=str(state_path), is_default=True,
    ))
    return wp


def _setup_two_level_game(tmp_path):
    """Create a game with 2 levels:
    Level 1: entrance→cp1→goal (ordinals 1, 2)
    Level 2: entrance→goal (ordinal 3)
    Returns (db, segment_ids_in_order).
    """
    db = Database(tmp_path / "sr.db")
    db.upsert_game("g", "Game", "any%")

    # Level 1 states
    l1_entrance = tmp_path / "l1_entrance.mss"
    l1_cp1 = tmp_path / "l1_cp1.mss"
    l1_entrance.write_bytes(b"state")
    l1_cp1.write_bytes(b"state")

    # Level 1 waypoints
    wp_l1_entrance = _make_waypoint_and_state(db, "g", 1, "entrance", 0, l1_entrance)
    wp_l1_cp1 = _make_waypoint_and_state(db, "g", 1, "checkpoint", 1, l1_cp1)
    wp_l1_goal = Waypoint.make("g", 1, "goal", 0, {})
    db.upsert_waypoint(wp_l1_goal)

    # Level 1 segments
    seg1 = Segment(
        id=Segment.make_id("g", 1, "entrance", 0, "checkpoint", 1, wp_l1_entrance.id, wp_l1_cp1.id),
        game_id="g", level_number=1,
        start_type="entrance", start_ordinal=0,
        end_type="checkpoint", end_ordinal=1,
        description="L1 start>cp1", ordinal=1,
        start_waypoint_id=wp_l1_entrance.id, end_waypoint_id=wp_l1_cp1.id,
    )
    seg2 = Segment(
        id=Segment.make_id("g", 1, "checkpoint", 1, "goal", 0, wp_l1_cp1.id, wp_l1_goal.id),
        game_id="g", level_number=1,
        start_type="checkpoint", start_ordinal=1,
        end_type="goal", end_ordinal=0,
        description="L1 cp1>goal", ordinal=2,
        start_waypoint_id=wp_l1_cp1.id, end_waypoint_id=wp_l1_goal.id,
    )
    db.upsert_segment(seg1)
    db.upsert_segment(seg2)

    # Level 2 states
    l2_entrance = tmp_path / "l2_entrance.mss"
    l2_entrance.write_bytes(b"state")

    wp_l2_entrance = _make_waypoint_and_state(db, "g", 2, "entrance", 0, l2_entrance)
    wp_l2_goal = Waypoint.make("g", 2, "goal", 0, {})
    db.upsert_waypoint(wp_l2_goal)

    seg3 = Segment(
        id=Segment.make_id("g", 2, "entrance", 0, "goal", 0, wp_l2_entrance.id, wp_l2_goal.id),
        game_id="g", level_number=2,
        start_type="entrance", start_ordinal=0,
        end_type="goal", end_ordinal=0,
        description="L2 start>goal", ordinal=3,
        start_waypoint_id=wp_l2_entrance.id, end_waypoint_id=wp_l2_goal.id,
    )
    db.upsert_segment(seg3)

    return db, [seg1.id, seg2.id, seg3.id]


@pytest.fixture
def sr_db(tmp_path):
    db, seg_ids = _setup_two_level_game(tmp_path)
    db._seg_ids = seg_ids
    db._tmp_path = tmp_path
    return db


def test_speed_run_builds_level_sequence(sr_db):
    """SpeedRunSession should group segments into levels ordered by ordinal."""
    tcp = AsyncMock()
    tcp.is_connected = True
    sr = SpeedRunSession(tcp=tcp, db=sr_db, game_id="g")
    levels = sr.levels

    assert len(levels) == 2
    # Level 1 has 2 segments, level 2 has 1
    assert len(levels[0].segments) == 2
    assert len(levels[1].segments) == 1
    # Level 1 has 1 checkpoint save state
    assert len(levels[0].checkpoints) == 1


def test_speed_run_refuses_missing_state(tmp_path):
    """SpeedRunSession should raise if any segment has no save state."""
    db = Database(tmp_path / "sr.db")
    db.upsert_game("g", "Game", "any%")

    wp_start = Waypoint.make("g", 1, "entrance", 0, {})
    wp_end = Waypoint.make("g", 1, "goal", 0, {})
    db.upsert_waypoint(wp_start)
    db.upsert_waypoint(wp_end)
    seg = Segment(
        id=Segment.make_id("g", 1, "entrance", 0, "goal", 0, wp_start.id, wp_end.id),
        game_id="g", level_number=1,
        start_type="entrance", start_ordinal=0,
        end_type="goal", end_ordinal=0,
        ordinal=1,
        start_waypoint_id=wp_start.id, end_waypoint_id=wp_end.id,
    )
    db.upsert_segment(seg)
    # No save state added — entrance has no file

    tcp = AsyncMock()
    tcp.is_connected = True
    with pytest.raises(ValueError, match="Missing save state"):
        SpeedRunSession(tcp=tcp, db=db, game_id="g")


@pytest.mark.asyncio
async def test_speed_run_sends_level_load(sr_db):
    """First run_one should send speed_run_load for level 1."""
    tcp = AsyncMock()
    tcp.is_connected = True

    sr = SpeedRunSession(tcp=tcp, db=sr_db, game_id="g")
    sr.is_running = True

    # Simulate goal completion after a short delay
    async def deliver():
        await asyncio.sleep(0.05)
        sr.receive_event({
            "event": "speed_run_complete",
            "elapsed_ms": 30000,
            "split_ms": 30000,
        })

    asyncio.create_task(deliver())
    result = await sr.run_one()

    assert result is True
    tcp.send_command.assert_called_once()
    cmd = tcp.send_command.call_args[0][0]
    assert cmd.event == "speed_run_load"
    assert len(cmd.checkpoints) == 1  # cp1
    assert cmd.checkpoints[0]["ordinal"] == 1


@pytest.mark.asyncio
async def test_speed_run_cold_recording_on_checkpoint(sr_db):
    """Checkpoint hit after cold start should record an attempt."""
    tcp = AsyncMock()
    tcp.is_connected = True

    sr = SpeedRunSession(tcp=tcp, db=sr_db, game_id="g")
    sr.is_running = True

    # Simulate: checkpoint hit (cold, from entrance load) then goal (hot)
    async def deliver():
        await asyncio.sleep(0.05)
        sr.receive_event({
            "event": "speed_run_checkpoint",
            "ordinal": 1,
            "elapsed_ms": 12000,
            "split_ms": 12000,
        })
        await asyncio.sleep(0.05)
        sr.receive_event({
            "event": "speed_run_complete",
            "elapsed_ms": 30000,
            "split_ms": 18000,
        })

    asyncio.create_task(deliver())
    await sr.run_one()

    # Should have recorded 1 cold attempt (entrance→cp1)
    # The goal was hot so no recording for cp1→goal
    seg_ids = sr_db._seg_ids
    attempts = sr_db.get_segment_attempts(seg_ids[0])  # entrance→cp1
    assert len(attempts) == 1
    assert attempts[0]["completed"] == 1
    assert attempts[0]["time_ms"] == 12000

    # cp1→goal should have 0 attempts (hot)
    attempts2 = sr_db.get_segment_attempts(seg_ids[1])
    assert len(attempts2) == 0


@pytest.mark.asyncio
async def test_speed_run_death_makes_next_segment_cold(sr_db):
    """Death should mark next sub-segment as cold for recording."""
    tcp = AsyncMock()
    tcp.is_connected = True

    sr = SpeedRunSession(tcp=tcp, db=sr_db, game_id="g")
    sr.is_running = True

    # Simulate: cp1 hit (cold) → death → cp1 hit again (cold from respawn) → cp2/goal
    async def deliver():
        await asyncio.sleep(0.02)
        # First cp1: cold from entrance
        sr.receive_event({
            "event": "speed_run_checkpoint",
            "ordinal": 1,
            "elapsed_ms": 12000,
            "split_ms": 12000,
        })
        await asyncio.sleep(0.02)
        # Death after cp1
        sr.receive_event({
            "event": "speed_run_death",
            "elapsed_ms": 18000,
            "split_ms": 6000,
        })
        await asyncio.sleep(0.02)
        # Goal after respawn at cp1 (cold)
        sr.receive_event({
            "event": "speed_run_complete",
            "elapsed_ms": 40000,
            "split_ms": 15000,
        })

    asyncio.create_task(deliver())
    await sr.run_one()

    seg_ids = sr_db._seg_ids
    # entrance→cp1: 1 cold attempt
    assert len(sr_db.get_segment_attempts(seg_ids[0])) == 1
    # cp1→goal: 1 cold attempt (from death respawn at cp1)
    attempts = sr_db.get_segment_attempts(seg_ids[1])
    assert len(attempts) == 1
    assert attempts[0]["time_ms"] == 15000


@pytest.mark.asyncio
async def test_speed_run_stops_after_last_level(sr_db):
    """Session should return False after last level completes."""
    tcp = AsyncMock()
    tcp.is_connected = True

    sr = SpeedRunSession(tcp=tcp, db=sr_db, game_id="g")
    sr.is_running = True

    # Complete level 1
    async def deliver_l1():
        await asyncio.sleep(0.02)
        sr.receive_event({"event": "speed_run_complete", "elapsed_ms": 30000, "split_ms": 30000})
    asyncio.create_task(deliver_l1())
    result1 = await sr.run_one()
    assert result1 is True

    # Complete level 2
    async def deliver_l2():
        await asyncio.sleep(0.02)
        sr.receive_event({"event": "speed_run_complete", "elapsed_ms": 20000, "split_ms": 20000})
    asyncio.create_task(deliver_l2())
    result2 = await sr.run_one()
    assert result2 is True

    # No more levels
    result3 = await sr.run_one()
    assert result3 is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_speed_run_mode.py -v`
Expected: FAIL — `speed_run` module doesn't exist

- [ ] **Step 3: Implement SpeedRunSession**

Create `python/spinlab/speed_run.py`:

```python
"""Speed Run session — sequential full-game playthrough with cold recording."""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Callable

from .models import Attempt
from .protocol import SpeedRunLoadCmd, SpeedRunStopCmd

if TYPE_CHECKING:
    from .db import Database
    from .tcp_manager import TcpManager

logger = logging.getLogger(__name__)

EVENT_WAIT_TIMEOUT_S = 1.0


@dataclass
class LevelPlan:
    """One level's worth of segments and checkpoint save states."""
    level_number: int
    description: str
    entrance_state_path: str
    # Segment IDs in order: [entrance→cp1, cp1→cp2, ..., cpN→goal]
    segments: list[dict] = field(default_factory=list)
    # CP save states in order: [{"ordinal": 1, "state_path": "..."}, ...]
    checkpoints: list[dict] = field(default_factory=list)


class SpeedRunSession:
    """Manages a speed run: plays levels sequentially, records cold attempts."""

    def __init__(
        self,
        tcp: "TcpManager",
        db: "Database",
        game_id: str,
        auto_advance_delay_ms: int = 1000,
        on_event: Callable | None = None,
    ) -> None:
        self.tcp = tcp
        self.db = db
        self.game_id = game_id
        self.auto_advance_delay_ms = auto_advance_delay_ms
        self.on_event = on_event

        self.session_id = uuid.uuid4().hex
        self.started_at = datetime.now(UTC).isoformat()
        self.is_running = False
        self.current_level_index = 0
        self.levels_completed = 0
        self.segments_recorded = 0

        # Build level sequence from DB
        self.levels = self._build_levels()

        # Event communication
        self._event_queue: asyncio.Queue = asyncio.Queue()

    def _build_levels(self) -> list[LevelPlan]:
        """Query segments, group into levels, validate save states exist."""
        rows = self.db.get_all_segments_with_model(self.game_id)
        if not rows:
            return []

        # Group segments by level — segments sharing consecutive ordinals
        # with the same level_number belong to the same level.
        # We detect level boundaries by looking at start_type == "entrance".
        levels: list[LevelPlan] = []
        current_level_segs: list[dict] = []

        for row in rows:
            if row["start_type"] == "entrance" and current_level_segs:
                # New level starts — finalize previous
                levels.append(self._finalize_level(current_level_segs))
                current_level_segs = []
            current_level_segs.append(row)

        if current_level_segs:
            levels.append(self._finalize_level(current_level_segs))

        return levels

    def _finalize_level(self, segs: list[dict]) -> LevelPlan:
        """Build a LevelPlan from a group of consecutive segments."""
        entrance_seg = segs[0]
        entrance_state = entrance_seg.get("state_path")
        if not entrance_state or not os.path.exists(entrance_state):
            desc = entrance_seg.get("description") or f"L{entrance_seg['level_number']}"
            raise ValueError(
                f"Missing save state for segment {entrance_seg['id']} ({desc})"
            )

        # Build checkpoint list from non-entrance segments' start waypoints
        checkpoints = []
        for seg in segs[1:]:
            cp_state = seg.get("state_path")
            if not cp_state or not os.path.exists(cp_state):
                desc = seg.get("description") or f"L{seg['level_number']}"
                raise ValueError(
                    f"Missing save state for segment {seg['id']} ({desc})"
                )
            checkpoints.append({
                "ordinal": seg["start_ordinal"],
                "state_path": cp_state,
            })

        description = entrance_seg.get("description") or f"Level {entrance_seg['level_number']}"

        return LevelPlan(
            level_number=entrance_seg["level_number"],
            description=description,
            entrance_state_path=entrance_state,
            segments=segs,
            checkpoints=checkpoints,
        )

    def start(self) -> None:
        self.db.create_session(self.session_id, self.game_id)
        self.is_running = True
        self.current_level_index = 0
        logger.info("speed_run: started session=%s levels=%d",
                     self.session_id[:8], len(self.levels))

    def stop(self) -> None:
        self.is_running = False
        self.db.end_session(
            self.session_id, self.segments_recorded, self.levels_completed,
        )
        logger.info("speed_run: stopped session=%s levels_completed=%d recorded=%d",
                     self.session_id[:8], self.levels_completed, self.segments_recorded)

    def receive_event(self, event: dict) -> None:
        """Called by SessionManager when a speed_run_* event arrives."""
        self._event_queue.put_nowait(event)

    async def run_one(self) -> bool:
        """Play one level. Returns False if no more levels."""
        if self.current_level_index >= len(self.levels):
            return False

        level = self.levels[self.current_level_index]

        cmd = SpeedRunLoadCmd(
            id=level.segments[0]["id"],
            state_path=level.entrance_state_path,
            description=level.description,
            checkpoints=level.checkpoints,
            auto_advance_delay_ms=self.auto_advance_delay_ms,
        )

        logger.info("speed_run: loading level %d/%d — %s",
                     self.current_level_index + 1, len(self.levels), level.description)
        await self.tcp.send_command(cmd)

        # Track cold state for recording
        cold_since = True
        # Index into level.segments: which sub-segment we're currently in
        current_sub_index = 0

        # Process events until level completes
        while self.is_running and self.tcp.is_connected:
            try:
                event = await asyncio.wait_for(
                    self._event_queue.get(), timeout=EVENT_WAIT_TIMEOUT_S
                )
            except asyncio.TimeoutError:
                continue

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
                # Death — next sub-segment will be cold
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

        if self.on_event:
            self.on_event(None)

        return True

    def _record_attempt(self, seg: dict, time_ms: int, completed: bool) -> None:
        """Record a cold attempt for a sub-segment."""
        attempt = Attempt(
            segment_id=seg["id"],
            session_id=self.session_id,
            completed=completed,
            time_ms=time_ms if completed else None,
            deaths=0,
            clean_tail_ms=time_ms if completed else None,
            source="speed_run",
        )
        self.db.log_attempt(attempt)
        self.segments_recorded += 1
        logger.info("speed_run: recorded cold attempt segment=%s time=%dms",
                     seg["id"], time_ms)

    async def run_loop(self) -> None:
        """Run the full speed run until stopped or all levels done."""
        self.start()
        try:
            while self.is_running and self.tcp.is_connected:
                if not await self.run_one():
                    break
        finally:
            try:
                await self.tcp.send_command(SpeedRunStopCmd())
            except (ConnectionError, OSError):
                pass
            self.stop()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_speed_run_mode.py -v`
Expected: PASS

- [ ] **Step 5: Run full fast tests**

Run: `python -m pytest -m "not (emulator or slow or frontend)" -q`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/speed_run.py tests/test_speed_run_mode.py
git commit -m "feat: add SpeedRunSession with level sequencing and cold recording"
```

---

### Task 4: SessionManager integration — start/stop/event routing

**Files:**
- Modify: `python/spinlab/session_manager.py`
- Modify: `python/spinlab/state_builder.py`
- Test: `tests/test_speed_run_mode.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_speed_run_mode.py`:

```python
from spinlab.session_manager import SessionManager
from spinlab.models import Mode, ActionResult, Status


@pytest.fixture
def session_mgr(sr_db, tmp_path):
    tcp = AsyncMock()
    tcp.is_connected = True
    tcp.send_command = AsyncMock()
    mgr = SessionManager(
        db=sr_db, tcp=tcp, rom_dir=tmp_path, data_dir=tmp_path,
    )
    mgr.game_id = "g"
    mgr.game_name = "Game"
    return mgr


@pytest.mark.asyncio
async def test_session_manager_start_speed_run(session_mgr):
    result = await session_mgr.start_speed_run()
    assert result.status == Status.STARTED
    assert session_mgr.mode == Mode.SPEED_RUN
    assert session_mgr.speed_run_session is not None


@pytest.mark.asyncio
async def test_session_manager_stop_speed_run(session_mgr):
    await session_mgr.start_speed_run()
    result = await session_mgr.stop_speed_run()
    assert result.status == Status.STOPPED
    assert session_mgr.mode == Mode.IDLE


@pytest.mark.asyncio
async def test_speed_run_routes_checkpoint_event(session_mgr):
    await session_mgr.start_speed_run()
    # Route a checkpoint event — should not crash
    await session_mgr.route_event({
        "event": "speed_run_checkpoint",
        "ordinal": 1,
        "elapsed_ms": 12000,
        "split_ms": 12000,
    })
    # Session should still be in speed_run mode
    assert session_mgr.mode == Mode.SPEED_RUN
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_speed_run_mode.py::test_session_manager_start_speed_run -v`
Expected: FAIL — `start_speed_run` doesn't exist

- [ ] **Step 3: Add speed run methods to SessionManager**

In `python/spinlab/session_manager.py`, add imports at the top:
```python
from .protocol import (
    # ... existing imports ...,
    SpeedRunCheckpointEvent, SpeedRunDeathEvent, SpeedRunCompleteEvent,
)
```

Add to `__init__`:
```python
self.speed_run_session = None  # SpeedRunSession | None
self.speed_run_task: asyncio.Task | None = None
```

Add to `_event_handlers` dict:
```python
SpeedRunCheckpointEvent: self._handle_speed_run_checkpoint,
SpeedRunDeathEvent: self._handle_speed_run_death,
SpeedRunCompleteEvent: self._handle_speed_run_complete,
```

Add methods:
```python
# --- Speed Run mode ---

async def start_speed_run(self) -> ActionResult:
    if self.capture.has_draft:
        return ActionResult(status=Status.DRAFT_PENDING)
    if self.speed_run_session and self.speed_run_session.is_running:
        return ActionResult(status=Status.ALREADY_RUNNING)
    if not self.tcp.is_connected:
        return ActionResult(status=Status.NOT_CONNECTED)
    if self.mode == Mode.REFERENCE:
        self._clear_ref_and_idle()

    from .speed_run import SpeedRunSession
    try:
        sr = SpeedRunSession(
            tcp=self.tcp, db=self.db, game_id=self._require_game(),
            on_event=lambda _: asyncio.ensure_future(self._notify_sse()),
        )
    except ValueError:
        return ActionResult(status=Status.MISSING_SAVE_STATES)

    self.speed_run_session = sr
    self.speed_run_task = asyncio.create_task(sr.run_loop())
    self.speed_run_task.add_done_callback(self._on_speed_run_done)
    self.mode = Mode.SPEED_RUN
    await self._notify_sse()
    return ActionResult(status=Status.STARTED, session_id=sr.session_id)

def _on_speed_run_done(self, task: asyncio.Task) -> None:
    if self.mode == Mode.SPEED_RUN:
        self.mode = Mode.IDLE
        asyncio.ensure_future(self._notify_sse())

async def stop_speed_run(self) -> ActionResult:
    if self.speed_run_session and self.speed_run_session.is_running:
        self.speed_run_session.is_running = False
        if self.speed_run_task:
            try:
                await asyncio.wait_for(self.speed_run_task, timeout=PRACTICE_STOP_TIMEOUT_S)
            except asyncio.TimeoutError:
                self.speed_run_task.cancel()
        self.mode = Mode.IDLE
        await self._notify_sse()
        return ActionResult(status=Status.STOPPED)
    if self.mode == Mode.SPEED_RUN:
        self.mode = Mode.IDLE
        return ActionResult(status=Status.STOPPED)
    return ActionResult(status=Status.NOT_RUNNING)

async def _handle_speed_run_checkpoint(self, event: SpeedRunCheckpointEvent) -> None:
    if self.mode != Mode.SPEED_RUN or not self.speed_run_session:
        return
    self.speed_run_session.receive_event(dataclasses.asdict(event))
    await self._notify_sse()

async def _handle_speed_run_death(self, event: SpeedRunDeathEvent) -> None:
    if self.mode != Mode.SPEED_RUN or not self.speed_run_session:
        return
    self.speed_run_session.receive_event(dataclasses.asdict(event))
    await self._notify_sse()

async def _handle_speed_run_complete(self, event: SpeedRunCompleteEvent) -> None:
    if self.mode != Mode.SPEED_RUN or not self.speed_run_session:
        return
    self.speed_run_session.receive_event(dataclasses.asdict(event))
    await self._notify_sse()
```

Update `on_disconnect`:
```python
def on_disconnect(self) -> None:
    if self.practice_session and self.practice_session.is_running:
        self.practice_session.is_running = False
    if self.speed_run_session and self.speed_run_session.is_running:
        self.speed_run_session.is_running = False
    self.cold_fill.clear()
    self.capture.handle_disconnect()
    self._clear_ref_and_idle()
```

Update `shutdown`:
```python
async def shutdown(self) -> None:
    await self.stop_practice()
    await self.stop_speed_run()
    if self.mode == Mode.REFERENCE:
        self._clear_ref_and_idle()
    await self.tcp.disconnect()
```

- [ ] **Step 4: Update StateBuilder for speed run state**

In `python/spinlab/state_builder.py`, add after the practice state block (line 52):

```python
if session.mode == Mode.SPEED_RUN and session.speed_run_session:
    self._build_speed_run_state(base, session)
```

Add method:
```python
def _build_speed_run_state(self, base: dict, session: "SessionManager") -> None:
    """Populate speed-run-specific fields into state dict."""
    sr = session.speed_run_session
    base["session"] = {
        "id": sr.session_id,
        "started_at": sr.started_at,
        "segments_attempted": sr.segments_recorded,
        "segments_completed": sr.levels_completed,
        "saved_total_ms": None,
        "saved_clean_ms": None,
    }
    if sr.current_level_index < len(sr.levels):
        level = sr.levels[sr.current_level_index]
        base["current_segment"] = {
            "id": level.segments[0]["id"],
            "game_id": sr.game_id,
            "level_number": level.level_number,
            "start_type": "entrance",
            "start_ordinal": 0,
            "end_type": "goal",
            "end_ordinal": 0,
            "description": level.description,
            "attempt_count": 0,
            "model_outputs": {},
            "selected_model": "",
            "state_path": level.entrance_state_path,
        }
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_speed_run_mode.py -v`
Expected: PASS

- [ ] **Step 6: Run full fast tests**

Run: `python -m pytest -m "not (emulator or slow or frontend)" -q`
Expected: All pass

- [ ] **Step 7: Commit**

```bash
git add python/spinlab/session_manager.py python/spinlab/state_builder.py tests/test_speed_run_mode.py
git commit -m "feat: integrate SpeedRunSession into SessionManager with event routing"
```

---

### Task 5: API route

**Files:**
- Create: `python/spinlab/routes/speed_run.py`
- Modify: `python/spinlab/dashboard.py` (register router)
- Test: `tests/test_speed_run_mode.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_speed_run_mode.py`:

```python
@pytest.mark.asyncio
async def test_speed_run_start_returns_error_missing_states(session_mgr):
    """start_speed_run should fail gracefully if states are missing."""
    # This uses the sr_db fixture which has valid states, so it should work.
    # Test the error path separately by removing a state file.
    import os
    path = session_mgr.db._tmp_path / "l1_entrance.mss"
    os.remove(path)
    result = await session_mgr.start_speed_run()
    assert result.status == Status.MISSING_SAVE_STATES
```

- [ ] **Step 2: Run test to verify it fails or passes (validation of error path)**

Run: `python -m pytest tests/test_speed_run_mode.py::test_speed_run_start_returns_error_missing_states -v`

- [ ] **Step 3: Create the route file**

Create `python/spinlab/routes/speed_run.py`:

```python
"""Speed Run start/stop routes."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from spinlab.dashboard import _check_result
from spinlab.session_manager import SessionManager

from ._deps import get_session

router = APIRouter(prefix="/api")


@router.post("/speedrun/start")
async def speed_run_start(session: SessionManager = Depends(get_session)):
    return _check_result(await session.start_speed_run())


@router.post("/speedrun/stop")
async def speed_run_stop(session: SessionManager = Depends(get_session)):
    return _check_result(await session.stop_speed_run())
```

- [ ] **Step 4: Register the router in dashboard.py**

In `python/spinlab/dashboard.py`, add alongside the other router imports and registrations:

```python
from .routes.speed_run import router as speed_run_router
# ...
app.include_router(speed_run_router)
```

- [ ] **Step 5: Run full fast tests**

Run: `python -m pytest -m "not (emulator or slow or frontend)" -q`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/routes/speed_run.py python/spinlab/dashboard.py tests/test_speed_run_mode.py
git commit -m "feat: add /api/speedrun/start and /api/speedrun/stop routes"
```

---

### Task 6: Frontend — buttons and state handling

**Files:**
- Modify: `frontend/src/types.ts` (add `speed_run` to Mode union)
- Modify: `frontend/src/model-logic.ts` (add `canStartSpeedRun`)
- Modify: `frontend/src/model.ts` (add buttons, show/hide logic)
- Modify: `frontend/src/header.ts` (speed run chip + stop)
- Modify: `frontend/index.html` (add buttons)
- Test: `frontend/src/model-logic.test.ts`

- [ ] **Step 1: Write the failing test**

Add to `frontend/src/model-logic.test.ts`:

```typescript
import { canStartSpeedRun } from "./model-logic";

test("canStartSpeedRun returns true when idle and connected", () => {
  const state = {
    mode: "idle" as const,
    tcp_connected: true,
    game_id: "g",
    game_name: "Game",
    current_segment: null,
    recent: [],
    session: null,
    sections_captured: 0,
    allocator_weights: null,
    estimator: null,
    capture_run_id: null,
    draft: null,
    cold_fill: null,
  };
  expect(canStartSpeedRun(state)).toBe(true);
});

test("canStartSpeedRun returns false during practice", () => {
  const state = {
    mode: "practice" as const,
    tcp_connected: true,
    game_id: "g",
    game_name: "Game",
    current_segment: null,
    recent: [],
    session: null,
    sections_captured: 0,
    allocator_weights: null,
    estimator: null,
    capture_run_id: null,
    draft: null,
    cold_fill: null,
  };
  expect(canStartSpeedRun(state)).toBe(false);
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npm test`
Expected: FAIL — `canStartSpeedRun` not exported

- [ ] **Step 3: Add `speed_run` to types and model-logic**

In `frontend/src/types.ts`, update Mode:
```typescript
export type Mode =
  | "idle"
  | "reference"
  | "practice"
  | "replay"
  | "fill_gap"
  | "cold_fill"
  | "speed_run";
```

In `frontend/src/model-logic.ts`, add:
```typescript
export function canStartSpeedRun(state: AppState): boolean {
  return state.tcp_connected && state.game_id !== null && state.mode === "idle";
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend && npm test`
Expected: PASS

- [ ] **Step 5: Add buttons to HTML**

In `frontend/index.html`, update the practice controls div:
```html
<div id="practice-controls" class="practice-controls">
  <button id="btn-practice-start" class="btn-primary">Start Practice</button>
  <button id="btn-practice-stop" class="btn-danger" style="display:none">Stop Practice</button>
  <button id="btn-speedrun-start" class="btn-primary">Start Speed Run</button>
  <button id="btn-speedrun-stop" class="btn-danger" style="display:none">Stop Speed Run</button>
</div>
```

- [ ] **Step 6: Wire up buttons in model.ts**

In `frontend/src/model.ts`, import `canStartSpeedRun`:
```typescript
import { selectedEstimate, currentEstimate, formatTrend, canStartPractice, canStartSpeedRun } from "./model-logic";
```

Update `updatePracticeControls`:
```typescript
export function updatePracticeControls(data: AppState): void {
  const startBtn = document.getElementById("btn-practice-start") as HTMLButtonElement;
  const stopBtn = document.getElementById("btn-practice-stop") as HTMLElement;
  const srStartBtn = document.getElementById("btn-speedrun-start") as HTMLButtonElement;
  const srStopBtn = document.getElementById("btn-speedrun-stop") as HTMLElement;
  const isPracticing = data.mode === "practice";
  const isSpeedRun = data.mode === "speed_run";

  startBtn.style.display = isPracticing || isSpeedRun ? "none" : "";
  startBtn.disabled = !canStartPractice(data);
  stopBtn.style.display = isPracticing ? "" : "none";

  srStartBtn.style.display = isPracticing || isSpeedRun ? "none" : "";
  srStartBtn.disabled = !canStartSpeedRun(data);
  srStopBtn.style.display = isSpeedRun ? "" : "none";
}
```

Update `updatePracticeCard` to also show during speed_run:
```typescript
export function updatePracticeCard(data: AppState): void {
  const card = document.getElementById("practice-card") as HTMLElement;
  if ((data.mode !== "practice" && data.mode !== "speed_run") || !data.current_segment) {
    card.style.display = "none";
    return;
  }
  // ... rest unchanged
```

Also hide allocator weights during speed run. In `updatePracticeCard`, wrap the weight slider rendering:
```typescript
  const weightsEl = document.getElementById("allocator-weights") as HTMLElement;
  if (weightsEl) {
    weightsEl.style.display = data.mode === "speed_run" ? "none" : "";
  }
  if (data.allocator_weights && data.mode !== "speed_run") {
    renderWeightSlider(data.allocator_weights);
  }
```

In `initModelTab`, add speed run button listeners:
```typescript
document.getElementById("btn-speedrun-start")!.addEventListener("click", () =>
  postJSON("/api/speedrun/start"),
);
document.getElementById("btn-speedrun-stop")!.addEventListener("click", () =>
  postJSON("/api/speedrun/stop"),
);
```

- [ ] **Step 7: Update header for speed run mode**

In `frontend/src/header.ts`, add speed_run handling in `updateHeader` after the practice block:
```typescript
} else if (data.mode === "speed_run") {
  chip.classList.add("practicing");
  const seg = data.current_segment;
  label.textContent = "Speed Run" + (seg ? " — " + segmentName(seg) : "");
  stopBtn.style.display = "";
```

Update the stop button handler to also handle speed run:
```typescript
document.getElementById("mode-stop")!.addEventListener("click", async () => {
  const chip = document.getElementById("mode-chip")!;
  if (chip.classList.contains("recording"))
    await postJSON("/api/reference/stop");
  else if (chip.classList.contains("practicing")) {
    // Could be practice or speed_run — try both
    await postJSON("/api/practice/stop");
    await postJSON("/api/speedrun/stop");
  } else if (chip.classList.contains("replaying"))
    await postJSON("/api/replay/stop");
});
```

- [ ] **Step 8: Build frontend and run tests**

Run: `cd frontend && npm test && npm run build`
Expected: All pass, build succeeds

- [ ] **Step 9: Commit**

```bash
git add frontend/src/types.ts frontend/src/model-logic.ts frontend/src/model-logic.test.ts frontend/src/model.ts frontend/src/header.ts frontend/index.html
git commit -m "feat: add Speed Run buttons and state handling to frontend"
```

---

### Task 7: Lua — handle_speed_run state machine

**Files:**
- Modify: `lua/spinlab.lua`

This is the Lua-side implementation. The speed run state machine mirrors practice mode but with CP-aware respawning and split timing.

- [ ] **Step 1: Add speed_run state table alongside practice state**

After the `practice` table (around line 139), add:

```lua
-- Speed run state
local speed_run = {
    active = false,
    state = PSTATE_IDLE,
    segment = nil,          -- parsed speed_run_load payload
    start_ms = 0,           -- level start time
    split_ms = 0,           -- time of last save state load (for split_ms in events)
    elapsed_ms = 0,
    respawn_path = "",      -- current respawn state path (advances with CPs)
    cp_index = 0,           -- index into checkpoints array (0-based)
    result_start_ms = 0,    -- when PSTATE_RESULT was entered
    result_split_ms = 0,    -- split_ms at goal time (saved for the complete event)
    auto_advance_ms = AUTO_ADVANCE_DEFAULT_MS,
}

local function speed_run_reset()
    speed_run.active = false
    speed_run.state = PSTATE_IDLE
    speed_run.segment = nil
    speed_run.start_ms = 0
    speed_run.split_ms = 0
    speed_run.elapsed_ms = 0
    speed_run.respawn_path = ""
    speed_run.cp_index = 0
    speed_run.result_start_ms = 0
    speed_run.result_split_ms = 0
    speed_run.auto_advance_ms = AUTO_ADVANCE_DEFAULT_MS
    reset_transition_state()
end
```

- [ ] **Step 2: Add speed_run_load command parser**

Near `parse_practice_segment` (around line 326), add:

```lua
-- Parse a JSON array of checkpoint objects from speed_run_load.
-- Each element: {"ordinal": N, "state_path": "..."}
-- Returns a Lua array of tables.
local function parse_checkpoints(json_str)
  local arr_str = json_get_array(json_str, "checkpoints")
  if not arr_str or arr_str == "[]" then return {} end

  local result = {}
  -- Simple pattern: split on },{ and parse each object
  for obj in arr_str:gmatch('%{[^}]+%}') do
    local ordinal = json_get_num(obj, "ordinal") or 0
    local state_path = json_get_str(obj, "state_path") or ""
    result[#result + 1] = { ordinal = ordinal, state_path = state_path }
  end
  return result
end

local function parse_speed_run_segment(json_str)
  return {
    id                     = json_get_str(json_str, "id") or "",
    state_path             = json_get_str(json_str, "state_path") or "",
    description            = json_get_str(json_str, "description") or "",
    checkpoints            = parse_checkpoints(json_str),
    expected_time_ms       = json_get_num(json_str, "expected_time_ms"),
    auto_advance_delay_ms  = json_get_num(json_str, "auto_advance_delay_ms") or AUTO_ADVANCE_DEFAULT_MS,
  }
end
```

- [ ] **Step 3: Add the speed run state machine**

After `handle_practice` (around line 883), add:

```lua
-----------------------------------------------------------------------
-- SPEED RUN STATE MACHINE
-----------------------------------------------------------------------
local function handle_speed_run(curr)
  if speed_run.state == PSTATE_LOADING then
    speed_run.state    = PSTATE_PLAYING
    speed_run.start_ms = ts_ms()
    speed_run.split_ms = ts_ms()

  elseif speed_run.state == PSTATE_PLAYING then
    -- Death check (highest priority)
    if is_death_frame(curr) then
      -- Send death event with timing
      local elapsed = ts_ms() - speed_run.start_ms
      local split = ts_ms() - speed_run.split_ms
      if client then
        client:send(to_json({
          event      = "speed_run_death",
          elapsed_ms = math.floor(elapsed),
          split_ms   = math.floor(split),
        }) .. "\n")
      end
      -- Reload at current respawn point
      table.insert(pending_loads, speed_run.respawn_path)
      speed_run.split_ms = ts_ms()  -- reset split timer
      log("Speed run: death — reloading " .. speed_run.respawn_path)

    elseif check_checkpoint_hit(curr) then
      -- CP hit — advance respawn point, send event, keep playing
      local elapsed = ts_ms() - speed_run.start_ms
      local split = ts_ms() - speed_run.split_ms
      local cps = speed_run.segment.checkpoints
      speed_run.cp_index = speed_run.cp_index + 1
      if speed_run.cp_index <= #cps then
        speed_run.respawn_path = cps[speed_run.cp_index].state_path
        local ordinal = cps[speed_run.cp_index].ordinal
        if client then
          client:send(to_json({
            event      = "speed_run_checkpoint",
            ordinal    = ordinal,
            elapsed_ms = math.floor(elapsed),
            split_ms   = math.floor(split),
          }) .. "\n")
        end
        log("Speed run: checkpoint " .. ordinal .. " — " .. math.floor(elapsed) .. "ms")
      end

    elseif detect_finish(curr) or is_exit_frame(curr) then
      -- Goal/exit — level complete
      speed_run.elapsed_ms = ts_ms() - speed_run.start_ms
      local split = ts_ms() - speed_run.split_ms
      speed_run.state = PSTATE_RESULT
      speed_run.result_start_ms = ts_ms()
      speed_run.result_split_ms = split
      log("Speed run: GOAL — " .. math.floor(speed_run.elapsed_ms) .. "ms")
    end

  elseif speed_run.state == PSTATE_RESULT then
    local elapsed_in_result = ts_ms() - speed_run.result_start_ms
    if elapsed_in_result >= speed_run.auto_advance_ms then
      if client then
        client:send(to_json({
          event      = "speed_run_complete",
          elapsed_ms = math.floor(speed_run.elapsed_ms),
          split_ms   = math.floor(speed_run.result_split_ms),
        }) .. "\n")
      end
      speed_run_reset()
      log("Speed run: level complete, sent result")
    end
  end
end
```

- [ ] **Step 4: Add overlay drawing for speed run**

After `draw_practice_overlay` (around line 405), add:

```lua
local function draw_speed_run_overlay()
  if not speed_run.active then return end

  local label = speed_run.segment and speed_run.segment.description or "?"
  if label == "" then label = "?" end
  local compare_time = speed_run.segment and speed_run.segment.expected_time_ms

  if speed_run.state == PSTATE_PLAYING or speed_run.state == PSTATE_LOADING then
    local elapsed = ts_ms() - speed_run.start_ms
    draw_text(4, 2, "SR: " .. label, 0x00000000, 0xFF44DDFF)
    draw_timer_row(12, elapsed, compare_time)

  elseif speed_run.state == PSTATE_RESULT then
    draw_text(4, 2, "SR: " .. label, 0x00000000, 0xFF44DDFF)
    draw_timer_row(12, speed_run.elapsed_ms, compare_time, "Clear!")

    local remaining = speed_run.auto_advance_ms - (ts_ms() - speed_run.result_start_ms)
    local secs = string.format("%.1f", math.max(0, remaining / 1000))
    draw_text(4, 22, "Next in " .. secs .. "s", 0x00000000, 0xFF888888)
  end
end
```

- [ ] **Step 5: Wire speed_run_load command handler**

In the JSON command dispatch section (around line 1119, where `practice_load` is handled), add:

```lua
  elseif decoded_event == "speed_run_load" then
    speed_run.segment = parse_speed_run_segment(line)
    speed_run.auto_advance_ms = speed_run.segment.auto_advance_delay_ms or 2000
    speed_run.respawn_path = speed_run.segment.state_path
    speed_run.cp_index = 0
    speed_run.active = true
    speed_run.state = PSTATE_LOADING
    local sp = speed_run.segment.state_path
    if not sp or sp == "" then
      log("ERROR: No valid state_path for speed_run segment " .. (speed_run.segment.id or "?"))
      client:send("err:no_state_path\n")
      speed_run_reset()
    else
      table.insert(pending_loads, sp)
      speed_run.start_ms = ts_ms()
      speed_run.split_ms = ts_ms()
      client:send("ok:queued\n")
      log("Speed run load queued: " .. (speed_run.segment.id or "?"))
    end
  elseif decoded_event == "speed_run_stop" then
    speed_run_reset()
    pending_loads = {}
    client:send("ok\n")
    log("Speed run stopped")
```

- [ ] **Step 6: Call handle_speed_run and draw overlay in main loops**

Find where `handle_practice(curr)` is called in the main per-frame function (the cpuExec callback). Add alongside it:

```lua
if speed_run.active then
  handle_speed_run(curr)
end
```

Find where `draw_practice_overlay()` is called in the draw callback. Add alongside it:

```lua
draw_speed_run_overlay()
```

Also update `send_event` to suppress passive events during speed run (same as practice):

```lua
local function send_event(event)
  if not client then return end
  if practice.active then return end
  if speed_run.active then return end
  -- ... rest unchanged
end
```

Update `disconnect_cleanup` to also reset speed run:

```lua
local function disconnect_cleanup()
  if practice.active then
    practice_reset()
    -- ... existing cleanup
  end
  if speed_run.active then
    speed_run_reset()
    pending_loads     = {}
    pending_saves     = {}
    pending_reset     = true
    log("Speed run auto-cleared on disconnect — reset queued")
  end
end
```

- [ ] **Step 7: Run emulator tests to check Lua doesn't crash**

Run: `python -m pytest -m emulator -q`
Expected: All pass (existing tests still work)

- [ ] **Step 8: Commit**

```bash
git add lua/spinlab.lua
git commit -m "feat: add speed run Lua state machine with CP respawning and overlay"
```

---

### Task 8: Frontend build + full test suite

**Files:** None new — validation task

- [ ] **Step 1: Build frontend**

Run: `cd frontend && npm run build`
Expected: Succeeds, output in `python/spinlab/static/`

- [ ] **Step 2: Run frontend tests**

Run: `cd frontend && npm test`
Expected: All pass

- [ ] **Step 3: Run full Python test suite**

Run: `python -m pytest`
Expected: All pass

- [ ] **Step 4: Commit any fixes if needed**

```bash
git add -A
git commit -m "fix: address test failures from speed run integration"
```
