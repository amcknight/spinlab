# Reference event-level recording — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the reference recorder's `recorded_segment_times` aggregate-row-plus-finalize-time-synthesis path with live per-event writes to `attempts` during recording. Reference deaths become real first-class v07 data points with raw wall-clock deltas, not synthesized penalty math.

**Architecture:** The recorder buffers per-segment `EventAttempt` rows in memory during a segment, then flushes them atomically with the segment row at `_close_segment`. The `recorded_segment_times` table is dropped — its sole purpose was being drained at finalize, and that role goes away. Finalize becomes a pure "promote draft + activate" gesture.

**Tech Stack:** Python 3.11+, SQLite (forward-only migrations), pytest. Spec at `docs/superpowers/specs/2026-05-21-reference-event-level-recording-design.md`. Sibling cold-state-variant fix already landed (commit `5523b0b`).

**Worktree recommendation:** This refactor is code edits + unit tests + a DB-integration test — no emulator, no Playwright, no port binding. A worktree is safe and recommended. Invoke `superpowers:using-git-worktrees` at execution time.

**Known semantic side-effect (downstream of the spec):** The legacy `_roll_up_episode` adapter currently produces `time_ms = wall_clock` for reference attempts (because the seed shim subtracts a 3.2s-per-death penalty before splitting, and the roll-up adds it back). After this refactor, reference event rows carry raw deltas (no penalty subtracted at write time), so `_roll_up_episode` will return `time_ms = wall_clock + 3.2s*deaths` for reference episodes — same shape it already produces for practice episodes. `clean_tail_ms` will also include the respawn-animation wall-clock for the last death (previously excluded via `_last_spawn_ms` accounting). This is consistent with how practice already feeds the legacy adapter and is acceptable churn — the v07 segments model is what reads raw events, and that gets honest data either way.

---

## File Structure

**Created:**
- `python/spinlab/db/migrations/0004_drop_recorded_segment_times.sql` — single `DROP TABLE recorded_segment_times;`
- `tests/integration/test_reference_event_recording.py` — end-to-end TDD-driven test exercising a reference run with deaths through the `ReferenceController` API, asserting the expected event-row shape in `attempts`

**Modified:**
- `python/spinlab/capture/recorder.py` — add `_episode_id`, `_last_event_ms`, `_pending_events`; emit per-event `EventAttempt` rows on death/checkpoint/exit; flush at segment close inside `db.transaction()`; stop writing to `recorded_segment_times`
- `python/spinlab/capture/reference.py` — delete `_seed_reference_attempts`; remove `drain_recorded_segment_times_for_run` call from `finalize_run`; replace the per-row `seed: segment=...` log lines with a per-segment `recorder: flushed N events for segment=...` log at flush time
- `python/spinlab/capture/finalizer.py` — strip drain + seed from `atomic_save_and_finish_run`; transaction now contains only end-session + promote-draft + activate
- `python/spinlab/db/__init__.py` — remove `RecordedSegmentTimesMixin` from the `Database` composition
- `tests/unit/capture/test_recorder.py` — rewrite all six tests to assert event rows in `attempts` (not summary rows in `recorded_segment_times`)
- `tests/unit/capture/test_finalizer.py` — replace the two tests for the now-deleted drain/seed contract with tests pinning the new finalize contract (promote + activate; rollback on mid-transaction failure leaves the run draft)
- `tests/unit/capture/test_multi_session.py` — replace `db.add_recorded_segment_time(...)` calls with the recorder API (or with the equivalent direct-event-row inserts where the test is purely about finalize bookkeeping)
- `tests/integration/test_crash_recovery.py` — assert the crash-safety bound via `attempts` rows instead of `recorded_segment_times`

**Deleted:**
- `python/spinlab/db/recorded_segment_times.py`
- `tests/unit/db/test_db_recorded_segment_times.py`

---

## Task 1: Recorder writes per-event rows at segment close (the core change)

**Files:**
- Modify: `python/spinlab/capture/recorder.py`
- Modify: `tests/unit/capture/test_recorder.py`

This task swaps the recorder's write target in `_close_segment`. The existing `recorded_segment_times` write is replaced by per-event row writes to `attempts`. All in one atomic transaction with the segment upsert. Existing test_recorder.py tests are rewritten to assert the new shape.

- [ ] **Step 1: Write new failing tests for event-row writes in `tests/unit/capture/test_recorder.py`**

Replace the existing file's contents with the test-suite for the new behavior. Note that tests reading from `recorded_segment_times` are removed entirely; the new tests read from `attempts`.

```python
import pytest

from spinlab.capture import SegmentRecorder
from spinlab.condition_registry import ConditionRegistry
from spinlab.db import Database
from spinlab.protocol import CheckpointEvent, LevelEntranceEvent, LevelExitEvent


@pytest.fixture
def db():
    d = Database(":memory:")
    d.upsert_game("g1", "Game", "any%")
    d.create_capture_run("run1", "g1", "Test Run", kind="live")
    d.create_capture_session("sess1", "run1", 1)
    yield d
    d.close()


@pytest.fixture
def registry():
    return ConditionRegistry()


def _make_cap(db: Database, registry: ConditionRegistry,
              run_id: str = "run1", sess_id: str = "sess1") -> SegmentRecorder:
    cap = SegmentRecorder(db, registry)
    cap.capture_run_id = run_id
    cap.current_capture_session_id = sess_id
    return cap


def _events_for_run(db, run_id):
    """All event rows in attempts for this run, in insertion order."""
    rows = db.conn.execute(
        "SELECT segment_id, episode_id, outcome, time_ms, source "
        "FROM attempts WHERE capture_run_id = ? ORDER BY id", (run_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def test_clean_segment_writes_one_survived_event(db, registry):
    """Entrance at t=1000, exit at t=6000, no deaths → one `survived` event
    with time_ms=5000 (raw wall-clock from entrance to exit)."""
    cap = _make_cap(db, registry)
    cap.handle_entrance(LevelEntranceEvent(level=1, timestamp_ms=1000, state_path="/s.mss"))
    cap.handle_exit(LevelExitEvent(level=1, goal="normal", timestamp_ms=6000), "g1")

    events = _events_for_run(db, "run1")
    assert len(events) == 1
    assert events[0]["outcome"] == "survived"
    assert events[0]["time_ms"] == 5000
    assert events[0]["source"] == "reference"


def test_segment_with_one_death_writes_died_then_survived(db, registry):
    """Entrance at t=1000, death at t=3000, spawn at t=6000, exit at t=9000
    → 2 events sharing one episode_id:
       died with time_ms=2000 (3000-1000)
       survived with time_ms=6000 (9000-3000; includes respawn animation)."""
    cap = _make_cap(db, registry)
    cap.handle_entrance(LevelEntranceEvent(level=1, timestamp_ms=1000, state_path="/s.mss"))
    cap.handle_death(timestamp_ms=3000)
    cap.handle_spawn_timing(timestamp_ms=6000)
    cap.handle_exit(LevelExitEvent(level=1, goal="normal", timestamp_ms=9000), "g1")

    events = _events_for_run(db, "run1")
    assert len(events) == 2
    assert events[0]["outcome"] == "died"
    assert events[0]["time_ms"] == 2000
    assert events[1]["outcome"] == "survived"
    assert events[1]["time_ms"] == 6000
    assert events[0]["episode_id"] == events[1]["episode_id"]


def test_two_deaths_in_segment_write_three_events(db, registry):
    """Two deaths and a clean tail produce died/died/survived events sharing
    one episode_id; each time_ms is the wall-clock delta since the previous
    event (or since the segment start, for the first death)."""
    cap = _make_cap(db, registry)
    cap.handle_entrance(LevelEntranceEvent(level=1, timestamp_ms=1000, state_path="/s.mss"))
    cap.handle_death(timestamp_ms=2000)
    cap.handle_death(timestamp_ms=3000)
    cap.handle_spawn_timing(timestamp_ms=4000)
    cap.handle_exit(LevelExitEvent(level=1, goal="normal", timestamp_ms=6000), "g1")

    events = _events_for_run(db, "run1")
    assert [e["outcome"] for e in events] == ["died", "died", "survived"]
    assert [e["time_ms"] for e in events] == [1000, 1000, 3000]
    assert len({e["episode_id"] for e in events}) == 1, \
        "all events of one segment share one episode_id"


def test_checkpoint_closes_segment_and_starts_new_episode(db, registry):
    """Entrance → checkpoint → exit produces two segments. Each segment's
    closing event is `survived`. The two segments have distinct episode_ids
    (one fresh episode per segment-pass)."""
    cap = _make_cap(db, registry)
    cap.handle_entrance(LevelEntranceEvent(level=1, timestamp_ms=1000, state_path="/s.mss"))
    cap.handle_checkpoint(
        CheckpointEvent(level_num=1, cp_ordinal=1, timestamp_ms=4000),
        "g1",
    )
    cap.handle_exit(LevelExitEvent(level=1, goal="normal", timestamp_ms=7000), "g1")

    events = _events_for_run(db, "run1")
    assert len(events) == 2
    assert all(e["outcome"] == "survived" for e in events)
    assert events[0]["time_ms"] == 3000
    assert events[1]["time_ms"] == 3000
    assert events[0]["episode_id"] != events[1]["episode_id"]
    assert events[0]["segment_id"] != events[1]["segment_id"]


def test_abort_drops_in_flight_segment(db, registry):
    """LevelExitEvent with goal='abort' drops the pending segment without
    writing any event rows or creating a segment row."""
    cap = _make_cap(db, registry)
    cap.handle_entrance(LevelEntranceEvent(level=1, timestamp_ms=1000, state_path="/s.mss"))
    cap.handle_death(timestamp_ms=2000)
    cap.handle_exit(LevelExitEvent(level=1, goal="abort", timestamp_ms=3000), "g1")

    events = _events_for_run(db, "run1")
    assert events == []
    seg_count = db.conn.execute(
        "SELECT COUNT(*) FROM segments WHERE capture_run_id = 'run1'",
    ).fetchone()[0]
    assert seg_count == 0


def test_clear_drops_in_flight_buffer(db, registry):
    """clear() drops the in-flight segment's buffered events. Events from
    previously-closed segments stay in attempts (clear is per-session
    in-memory only, not a DB rollback)."""
    cap = _make_cap(db, registry)
    cap.handle_entrance(LevelEntranceEvent(level=1, timestamp_ms=0, state_path="/s.mss"))
    cap.handle_exit(LevelExitEvent(level=1, goal="normal", timestamp_ms=5000), "g1")

    # One segment closed → one event in attempts.
    assert len(_events_for_run(db, "run1")) == 1

    # Start a second segment but clear before closing → no second event.
    cap.handle_entrance(LevelEntranceEvent(level=2, timestamp_ms=6000, state_path="/s2.mss"))
    cap.handle_death(timestamp_ms=7000)
    cap.clear()

    # First segment's event still present; second segment's buffered events lost.
    events = _events_for_run(db, "run1")
    assert len(events) == 1


async def test_handle_spawn_event_does_not_emit_event_row(db, registry):
    """SpawnEvent is used by the recorder for save-state and detector wiring,
    but is NOT itself an event row in attempts. Regression guard for the
    multi-session work where spawn timing was conflated with event emission."""
    from tests.conftest import FakeEmuBackend

    from spinlab.capture.reference import ReferenceController
    from spinlab.protocol import (
        DeathEvent,
        LevelEntranceEvent,
        LevelExitEvent,
        SpawnEvent,
    )

    ctl = ReferenceController(db, FakeEmuBackend(connected=True))
    ctl.recorder.capture_run_id = "run1"
    ctl.recorder.current_capture_session_id = "sess1"

    await ctl.handle_entrance(LevelEntranceEvent(
        level=1, state_path=None, timestamp_ms=1000, conditions={},
    ))
    ctl.handle_death(DeathEvent())
    ctl.handle_spawn(SpawnEvent(
        level_num=1, state_path=None,
        is_cold_cp=False, cp_ordinal=None,
        timestamp_ms=3000, conditions={},
    ), game_id="g1")
    ctl.handle_exit(LevelExitEvent(
        level=1, goal="normal", timestamp_ms=5000, conditions={},
    ), game_id="g1")

    events = _events_for_run(db, "run1")
    # 2 events: died + survived. The spawn is NOT a row in attempts.
    assert [e["outcome"] for e in events] == ["died", "survived"]
```

- [ ] **Step 2: Run new tests to verify they fail**

Run: `python -m pytest tests/unit/capture/test_recorder.py -v`
Expected: All tests FAIL (the recorder doesn't yet write to `attempts`; the existing implementation writes to `recorded_segment_times` which the new tests don't read).

- [ ] **Step 3: Modify `SegmentRecorder` to write per-event rows at segment close**

Replace the relevant parts of `python/spinlab/capture/recorder.py`:

The full file becomes:

```python
"""SegmentRecorder — owns reference/replay segment capture state and logic.

Per-event attempt rows are buffered in memory during a segment, then flushed
to `attempts` atomically with the segment upsert at segment close. A dashboard
crash mid-segment loses the in-flight segment's buffered events; completed
segments are durable. Same crash-safety bound as the pre-2026-05 design
that drained `recorded_segment_times` at finalize.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ..models import (
    AttemptOutcome,
    AttemptSource,
    EndpointType,
    EventAttempt,
)
from ..protocol import (
    CheckpointEvent,
    LevelEntranceEvent,
    LevelExitEvent,
    SpawnEvent,
)

if TYPE_CHECKING:
    from ..condition_registry import ConditionRegistry
    from ..db import Database

logger = logging.getLogger(__name__)


@dataclass
class PendingStart:
    """Buffered start-of-segment state for pairing with the next endpoint."""
    type: EndpointType     # ENTRANCE or CHECKPOINT
    ordinal: int
    state_path: str | None
    timestamp_ms: int
    level_num: int
    raw_conditions: dict[str, int]


@dataclass
class _PendingEvent:
    """One died/survived event buffered until the segment closes (when the
    real `segment_id` becomes known and the rows can be written together).
    """
    outcome: AttemptOutcome
    time_ms: int
    created_at: datetime


class SegmentRecorder:
    """Captures segments during reference runs and replays.

    Stateless across recording sessions: created with a `capture_run_id` and a
    `current_capture_session_id`, and writes directly to the DB. Per-segment
    boundaries (death counts, episode_id, pending events) reset on `clear()`.
    """

    def __init__(
        self,
        db: "Database",
        condition_registry: "ConditionRegistry",
    ) -> None:
        self._db = db
        self._condition_registry = condition_registry
        self.capture_run_id: str | None = None
        self.current_capture_session_id: str | None = None
        self.pending_start: PendingStart | None = None
        self.died: bool = False
        self.rec_path: str | None = None
        self._deaths_in_segment: int = 0
        self._last_spawn_ms: int | None = None

        # Per-segment episode tracking (one fresh episode_id per segment-pass).
        # Minted at handle_entrance / handle_checkpoint(segment-start) and
        # carried into every EventAttempt buffered for this segment.
        self._episode_id: str = ""
        # Wall-clock of the previous event in this episode (or segment-start
        # before the first event). `time_ms` per event = now - _last_event_ms.
        self._last_event_ms: int = 0
        # Buffered events for the in-flight segment; flushed atomically with
        # the segment upsert at _close_segment.
        self._pending_events: list[_PendingEvent] = []

    def set_condition_registry(self, registry: "ConditionRegistry") -> None:
        """Swap the active condition registry (called on game-switch)."""
        self._condition_registry = registry

    def clear(self) -> None:
        """Reset per-session state. Does NOT clear DB rows."""
        self.capture_run_id = None
        self.current_capture_session_id = None
        self.pending_start = None
        self.died = False
        self.rec_path = None
        self._deaths_in_segment = 0
        self._last_spawn_ms = None
        self._episode_id = ""
        self._last_event_ms = 0
        self._pending_events = []

    def _arm_new_episode(self, start_ts_ms: int) -> None:
        """Mint a fresh episode_id for the upcoming segment and reset
        per-segment buffer/counters."""
        self._episode_id = uuid.uuid4().hex
        self._last_event_ms = start_ts_ms
        self._pending_events = []
        self._deaths_in_segment = 0
        self._last_spawn_ms = None
        self.died = False

    def handle_entrance(self, event: LevelEntranceEvent) -> None:
        """Buffer a level entrance as pending start."""
        if self.pending_start and self.pending_start.type != "entrance":
            logger.info("Ignoring level_entrance — pending start exists: %s",
                        self.pending_start)
            return
        self.pending_start = PendingStart(
            type=EndpointType.ENTRANCE, ordinal=0,
            state_path=event.state_path, timestamp_ms=event.timestamp_ms,
            level_num=event.level, raw_conditions=event.conditions,
        )
        self._arm_new_episode(event.timestamp_ms)

    def _close_segment(self, game_id, start: PendingStart, end_type, end_ordinal,
                       level, end_raw_conditions,
                       end_timestamp_ms: int | None = None) -> None:
        """Create waypoints + segment for the segment ending here, then flush
        all buffered events (deaths + closing survived) into `attempts` —
        atomically with the segment upsert.
        """
        from ..models import Segment, Waypoint, WaypointSaveState

        start_conds = self._condition_registry.decode(start.raw_conditions, level=level)
        end_conds = self._condition_registry.decode(end_raw_conditions, level=level)

        wp_start = Waypoint.make(game_id, level, start.type,
                                 start.ordinal, start_conds)
        wp_end = Waypoint.make(game_id, level, end_type, end_ordinal, end_conds)
        seg_id = Segment.make_id(
            game_id, level, start.type, start.ordinal,
            end_type, end_ordinal, wp_start.id, wp_end.id,
        )

        # Build the closing "survived" event before opening the transaction
        # so the buffer is fully assembled before we touch the DB.
        survived_event: _PendingEvent | None = None
        if (start.timestamp_ms is not None and end_timestamp_ms is not None
                and self.current_capture_session_id is not None
                and self.capture_run_id is not None):
            survived_event = _PendingEvent(
                outcome=AttemptOutcome.SURVIVED,
                time_ms=end_timestamp_ms - self._last_event_ms,
                created_at=datetime.now(UTC),
            )

        with self._db.transaction():
            self._db.upsert_waypoint(wp_start)
            self._db.upsert_waypoint(wp_end)
            is_primary = not self._db.has_competing_active_segment(
                game_id=game_id, level=level,
                start_type=start.type, start_ordinal=start.ordinal,
                end_type=end_type, end_ordinal=end_ordinal,
                exclude_segment_id=seg_id,
            )
            existing_count = (
                self._db.count_segments_for_run(self.capture_run_id)
                if self.capture_run_id else 0
            )
            seg = Segment(
                id=seg_id, game_id=game_id, level_number=level,
                start_type=start.type, start_ordinal=start.ordinal,
                end_type=end_type, end_ordinal=end_ordinal,
                start_waypoint_id=wp_start.id, end_waypoint_id=wp_end.id,
                is_primary=is_primary,
                ordinal=existing_count + 1,
                capture_run_id=self.capture_run_id,
                capture_session_id=self.current_capture_session_id,
            )
            self._db.upsert_segment(seg)

            state_path = start.state_path
            if state_path:
                variant = "cold" if start.type == "entrance" else "hot"
                self._db.add_save_state(WaypointSaveState(
                    waypoint_id=wp_start.id,
                    variant_type=variant,
                    state_path=state_path,
                ))

            # Flush buffered events + closing survived event. All keyed to
            # the just-upserted segment_id so the FK is satisfied.
            events_to_write = list(self._pending_events)
            if survived_event is not None:
                events_to_write.append(survived_event)
            if events_to_write and self.capture_run_id is not None:
                for ev in events_to_write:
                    self._db.log_event_attempt(EventAttempt(
                        segment_id=seg_id,
                        episode_id=self._episode_id,
                        outcome=ev.outcome,
                        time_ms=ev.time_ms,
                        capture_run_id=self.capture_run_id,
                        source=AttemptSource.REFERENCE,
                        created_at=ev.created_at,
                    ))
                logger.info(
                    "recorder: flushed %d events for segment=%s (deaths=%d)",
                    len(events_to_write), seg_id, self._deaths_in_segment,
                )

        self._pending_events = []
        self._deaths_in_segment = 0
        self._last_spawn_ms = None

    def handle_checkpoint(self, event: CheckpointEvent, game_id: str) -> None:
        if not self.pending_start:
            return
        cp_ordinal = event.cp_ordinal
        level = event.level_num if event.level_num else self.pending_start.level_num
        self._close_segment(
            game_id, self.pending_start, "checkpoint", cp_ordinal,
            level, event.conditions,
            end_timestamp_ms=event.timestamp_ms)
        self.pending_start = PendingStart(
            type=EndpointType.CHECKPOINT, ordinal=cp_ordinal,
            state_path=event.state_path, timestamp_ms=event.timestamp_ms,
            level_num=level, raw_conditions=event.conditions,
        )
        # New segment starts here — fresh episode for the cp→next pass.
        self._arm_new_episode(event.timestamp_ms)

    def handle_exit(self, event: LevelExitEvent, game_id: str) -> None:
        if event.goal == "abort":
            # Drop the in-flight buffer; the segment never closes so no
            # events get written.
            self.pending_start = None
            self._pending_events = []
            self._deaths_in_segment = 0
            self._last_spawn_ms = None
            return
        if not self.pending_start:
            return
        level = event.level
        self._close_segment(
            game_id, self.pending_start, "goal", 0,
            level, event.conditions,
            end_timestamp_ms=event.timestamp_ms)
        self.pending_start = None

    def handle_death(self, timestamp_ms: int | None = None) -> None:
        self.died = True
        self._deaths_in_segment += 1
        # Buffer the died event with raw wall-clock delta since the previous
        # event (or segment-start, for the first death). The penalty math
        # lives in the legacy roll-up adapter, not at write time — see
        # docs/superpowers/specs/2026-05-21-reference-event-level-recording-design.md.
        if timestamp_ms is not None:
            self._pending_events.append(_PendingEvent(
                outcome=AttemptOutcome.DIED,
                time_ms=timestamp_ms - self._last_event_ms,
                created_at=datetime.now(UTC),
            ))
            self._last_event_ms = timestamp_ms

    def handle_spawn_timing(self, timestamp_ms: int | None = None) -> None:
        if timestamp_ms is not None:
            self._last_spawn_ms = timestamp_ms

    def handle_spawn(self, event: SpawnEvent, game_id: str) -> None:
        if not event.is_cold_cp:
            return
        cold_path = event.state_path
        level = event.level_num
        cp_ord = event.cp_ordinal
        if cold_path is None or cp_ord is None:
            return
        from ..models import EndpointType, Waypoint, WaypointSaveState
        conds = self._condition_registry.decode(event.conditions, level=level)
        wp = Waypoint.make(game_id, level, EndpointType.CHECKPOINT, cp_ord, conds)
        self._db.upsert_waypoint(wp)
        self._db.add_save_state(WaypointSaveState(
            waypoint_id=wp.id, variant_type="cold",
            state_path=cold_path))
        logger.debug("Stored cold save state for waypoint %s: %s", wp.id, cold_path)
```

Key changes from the pre-refactor file:
- Added `_episode_id`, `_last_event_ms`, `_pending_events` fields and `_arm_new_episode` helper.
- `handle_entrance` now calls `_arm_new_episode` (folds in the old reset of `_deaths_in_segment` etc.).
- `handle_death` appends a `_PendingEvent(died)` to the buffer with delta-time math; updates `_last_event_ms`.
- `_close_segment` no longer calls `add_recorded_segment_time`. Instead, it appends a `_PendingEvent(survived)` and flushes the whole buffer via `db.log_event_attempt(EventAttempt(...))` per row, all inside `self._db.transaction()` so the segment row + events land atomically.
- `handle_checkpoint` calls `_arm_new_episode(event.timestamp_ms)` after closing the previous segment, so the cp→next segment starts a fresh episode.
- `handle_exit(goal='abort')` explicitly clears the in-flight buffer.

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `python -m pytest tests/unit/capture/test_recorder.py -v`
Expected: All tests in the new suite PASS.

- [ ] **Step 5: Run the full suite to catch breakage in dependents**

Run: `python -m pytest 2>&1 | tail -30`
Expected: Some failures in `test_finalizer.py`, `test_multi_session.py`, and `test_crash_recovery.py` (they read from `recorded_segment_times` or rely on the finalize seed path). These are fixed in later tasks; do NOT commit until Task 6 if any expected failures appear here. If failures appear OUTSIDE those files, stop and diagnose — they're unintended.

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/capture/recorder.py tests/unit/capture/test_recorder.py
git commit -m "$(cat <<'EOF'
recorder: write per-event attempt rows at segment close

Reference recorder now buffers EventAttempt rows in memory during a
segment and flushes them atomically with the segment upsert at
_close_segment, instead of writing one aggregate recorded_segment_times
row. Each event carries a raw wall-clock delta since the previous event
(no penalty math at write time — the legacy roll-up adds it back).

One fresh episode_id per segment-pass, minted at entrance or at the
checkpoint that starts a new segment. Same crash-safety bound as
before: in-flight segment lost on crash, completed segments durable.

Dependents (finalizer, multi-session, crash-recovery tests) will be
fixed in follow-up commits — they still read recorded_segment_times
which this commit stops writing.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

Note: this commit intentionally leaves the suite red (finalizer / multi-session / crash-recovery tests). Tasks 2–4 restore green incrementally.

---

## Task 2: Simplify finalize (drop drain + seed)

**Files:**
- Modify: `python/spinlab/capture/reference.py`
- Modify: `python/spinlab/capture/finalizer.py`
- Modify: `tests/unit/capture/test_finalizer.py`

`_seed_reference_attempts` and the drain/seed step in `atomic_save_and_finish_run` are now redundant — the recorder has already written event rows for every closed segment. Finalize becomes "promote draft + activate" only.

- [ ] **Step 1: Write/update tests in `tests/unit/capture/test_finalizer.py` for the new contract**

Replace the file's contents:

```python
"""Tests for atomic_save_and_finish_run.

The finalize path no longer drains recorded_segment_times or seeds
attempts — those rows were already written event-by-event by the
recorder. Finalize is now: end the capture_session, promote the
draft to saved, activate. These tests pin that contract.
"""
from __future__ import annotations

import pytest

from spinlab.models import Mode, Status


@pytest.mark.asyncio
async def test_happy_path_promotes_and_activates(reference_controller_recording):
    """Happy path: session ended, draft promoted to saved, run activated.
    No row movement (event rows already exist from the recorder)."""
    ctl = reference_controller_recording
    db = ctl.db
    run_id = ctl.recorder.capture_run_id
    sess_id = ctl.recorder.current_capture_session_id
    assert run_id is not None
    assert sess_id is not None

    result = await ctl.save_and_finish_run(Mode.REFERENCE, "Finalized Name")

    assert result.status == Status.OK
    assert result.new_mode == Mode.IDLE

    sess_row = db.conn.execute(
        "SELECT ended_at FROM capture_sessions WHERE id = ?", (sess_id,),
    ).fetchone()
    assert sess_row[0] is not None, "capture_session should be ended"

    cap = db.conn.execute(
        "SELECT status, name, active FROM capture_runs WHERE id = ?", (run_id,),
    ).fetchone()
    assert cap[0] == "saved", "status promoted to saved"
    assert cap[1] == "Finalized Name"
    assert cap[2] == 1, "run activated"


@pytest.mark.asyncio
async def test_rollback_on_mid_transaction_failure(
    reference_controller_recording, monkeypatch,
):
    """If any mutation in the finalize transaction raises, every prior
    mutation rolls back: draft stays 1, name unchanged, capture_session
    not ended."""
    ctl = reference_controller_recording
    db = ctl.db
    run_id = ctl.recorder.capture_run_id
    sess_id = ctl.recorder.current_capture_session_id
    assert run_id is not None
    assert sess_id is not None

    real_conn = db.conn

    class FailingConn:
        def execute(self, sql, *args, **kwargs):
            # Fail on the activation step (UPDATE capture_runs SET active=1).
            # This is the last mutation in the transaction; failing here
            # exercises the full rollback of session-end + promote-draft.
            if "SET active = 1" in sql or "SET active=1" in sql:
                raise RuntimeError("injected failure mid-transaction")
            return real_conn.execute(sql, *args, **kwargs)

        def commit(self):
            return real_conn.commit()

        def rollback(self):
            return real_conn.rollback()

        @property
        def in_transaction(self):
            return real_conn.in_transaction

    monkeypatch.setattr(db, "conn", FailingConn())

    with pytest.raises(RuntimeError, match="injected failure"):
        await ctl.save_and_finish_run(Mode.REFERENCE, "Test Name")

    monkeypatch.undo()

    row = db.conn.execute(
        "SELECT status, name FROM capture_runs WHERE id = ?", (run_id,),
    ).fetchone()
    assert row[0] == "draft", "status rolled back to draft"
    assert row[1] == "In-Progress", "name unchanged"

    sess_row = db.conn.execute(
        "SELECT ended_at FROM capture_sessions WHERE id = ?", (sess_id,),
    ).fetchone()
    assert sess_row[0] is None, "capture_session end rolled back"
```

- [ ] **Step 2: Run the new finalize tests to verify they fail**

Run: `python -m pytest tests/unit/capture/test_finalizer.py -v`
Expected: FAIL. The current `atomic_save_and_finish_run` still calls `drain_recorded_segment_times_for_run` (which exists) but the rollback-failure test now triggers on a different SQL pattern; the happy-path test no longer expects any seeded attempts.

- [ ] **Step 3: Modify `python/spinlab/capture/finalizer.py` to drop drain + seed**

Replace the file's contents:

```python
"""atomic_save_and_finish_run — atomic finalize of a recording capture_run.

Three mutations happen inside one ``db.transaction()``: end the capture
session, promote the draft to saved, activate this run (deactivating
sibling runs for the same game). Either every step succeeds and commits,
or any failure rolls back and re-raises.

Event rows for the captured segments were already written by the
SegmentRecorder as each segment closed; finalize does not touch
`attempts` at all. The pre-2026-05 drain-and-seed step is gone with
the `recorded_segment_times` table.

Caller is responsible for the recorder-state transition to idle and
any scheduler.rebuild_all_states() call — those are not part of the
atomic unit.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from spinlab.db import Database

logger = logging.getLogger(__name__)


def atomic_save_and_finish_run(
    db: "Database",
    run_id: str,
    session_id: str | None,
    name: str,
) -> None:
    """End session + promote draft + activate. Atomic; any failure rolls back."""
    with db.transaction():
        if session_id:
            db.end_capture_session(session_id, end_reason="stopped")
        db.promote_draft(run_id, name)
        db.set_active_capture_run(run_id)
```

- [ ] **Step 4: Modify `python/spinlab/capture/reference.py` to drop `_seed_reference_attempts` and simplify finalize**

Apply two changes:

(a) Delete the entire `_seed_reference_attempts` function (lines ~67-91) AND the unused imports it introduced. The remaining imports at the top of the file should drop `Attempt`, `AttemptSource`, `_dt`, `Sequence`, and `RecordedSegmentTimeRow` if no other code in the file uses them. After the edit, the import block should read (excerpt):

```python
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from ..condition_registry import ConditionRegistry
from ..errors import (
    AlreadyReplayingError,
    DraftPendingError,
    NoPausedRunError,
    NotConnectedError,
    NotInReferenceError,
    NotReplayingError,
    PracticeActiveError,
    ReferenceActiveError,
    SessionDeleteAfterFinalizeError,
    SessionInUseError,
)
from ..models import (
    ActionResult,
    Mode,
    Status,
)
from ..protocol import (
    SPEED_UNCAPPED,
    CheckpointEvent,
    DeathEvent,
    LevelEntranceEvent,
    LevelExitEvent,
    ReferenceStartCmd,
    ReferenceStopCmd,
    ReplayCmd,
    ReplayStartedEvent,
    ReplayStopCmd,
    SpawnEvent,
)
from .recorder import SegmentRecorder

if TYPE_CHECKING:
    from ..db import Database
    from ..emu_backend import EmuBackend
    from ..scheduler import Scheduler
```

(b) Replace `finalize_run` with the simpler version:

```python
    async def finalize_run(self, name: str, scheduler: "Scheduler | None" = None) -> ActionResult:
        if not self.paused_run_id:
            raise NoPausedRunError()
        run_id = self.paused_run_id
        self.db.promote_draft(run_id, name)
        self.db.set_active_capture_run(run_id)
        # Always rebuild after activation: set_active_capture_run changed
        # which reference the scheduler should reason about, regardless of
        # whether any new event rows landed during this finalize.
        if scheduler:
            scheduler.rebuild_all_states()
        self._enter_idle()
        logger.info("reference: finalized run=%s as %r", run_id, name)
        return ActionResult(status=Status.OK)
```

(c) Replace `save_and_finish_run` so it no longer iterates seeded attempts to log them:

```python
    async def save_and_finish_run(
        self, mode: Mode, name: str, scheduler: "Scheduler | None" = None,
    ) -> ActionResult:
        """Combined Stop Session + Finalize, atomic.

        Two valid entry conditions:
          - mode == REFERENCE: full atomic stop + finalize.
          - mode == IDLE and paused_run_id is set: the user already clicked
            Stop separately; just finalize the paused run. Delegates to
            finalize_run so the dashboard's primary "Save & Finish Run"
            button works regardless of whether the user clicked Stop first.

        Event rows for captured segments were already written by the recorder
        as each segment closed. Finalize just ends the session, promotes the
        draft, and activates.
        """
        if mode == Mode.IDLE and self.paused_run_id:
            return await self.finalize_run(name, scheduler=scheduler)
        if mode != Mode.REFERENCE:
            raise NotInReferenceError()
        if self.emu.is_connected:
            await self.emu.send_command(ReferenceStopCmd())

        sess_id = self.recorder.current_capture_session_id
        run_id = self.recorder.capture_run_id
        if not run_id:
            raise NoPausedRunError()

        from .finalizer import atomic_save_and_finish_run
        atomic_save_and_finish_run(self.db, run_id, sess_id, name)

        if scheduler:
            scheduler.rebuild_all_states()
        self._enter_idle()
        logger.info("reference: save_and_finish run=%s as %r", run_id, name)
        return ActionResult(status=Status.OK, new_mode=Mode.IDLE)
```

- [ ] **Step 5: Run the finalize tests to verify they pass**

Run: `python -m pytest tests/unit/capture/test_finalizer.py -v`
Expected: Both tests PASS.

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/capture/reference.py python/spinlab/capture/finalizer.py tests/unit/capture/test_finalizer.py
git commit -m "$(cat <<'EOF'
finalize: drop drain+seed (events already in attempts)

_seed_reference_attempts deleted; atomic_save_and_finish_run now only
ends the session, promotes the draft, and activates the run.
SegmentRecorder writes event rows directly as each segment closes,
so finalize has nothing to seed.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Update multi-session tests to use the recorder API

**Files:**
- Modify: `tests/unit/capture/test_multi_session.py`

Several tests insert directly into `recorded_segment_times` via `db.add_recorded_segment_time(...)` and then assert that finalize seeds them. With the new design, that helper is going away (Task 6) and finalize no longer seeds. The tests need to be rewritten to either:
- Use the recorder API to produce real event rows, OR
- Insert directly into `attempts` for tests that are purely about finalize bookkeeping (not about end-to-end event production).

- [ ] **Step 1: Update each failing test**

Apply the following edits to `tests/unit/capture/test_multi_session.py`:

(a) `test_save_and_finish_from_paused_after_stop_finalizes` — drop the `db.add_recorded_segment_time` call and the `_make_minimal_segment` call. The test's intent is "Save & Finish from IDLE finalizes the paused run"; no segments needed:

```python
@pytest.mark.asyncio
async def test_save_and_finish_from_paused_after_stop_finalizes(started_session, db):
    """Regression: clicking Save & Finish AFTER Stop should finalize the
    paused run, not silently 409. The dashboard's primary save button stays
    visible after Stop and users expect it to work either way."""
    run_id = started_session.recorder.capture_run_id

    # Stop first (mode goes REFERENCE → IDLE, run becomes paused).
    await started_session.stop_reference(Mode.REFERENCE)
    assert started_session.has_paused_run

    # Now Save & Finish from IDLE should finalize the paused run.
    result = await started_session.save_and_finish_run(Mode.IDLE, name="Stopped First")
    assert result.status == Status.OK
    row = db.conn.execute(
        "SELECT status, name FROM capture_runs WHERE id = ?", (run_id,)
    ).fetchone()
    assert row[0] == "saved"  # promoted from draft
    assert row[1] == "Stopped First"
```

(b) `test_save_and_finish_seeds_attempts_and_finalizes` — rename to reflect new contract and drive a real reference segment via the recorder:

```python
@pytest.mark.asyncio
async def test_save_and_finish_promotes_and_keeps_recorded_event_rows(started_session, db):
    """A reference segment recorded through the recorder leaves event rows
    in `attempts`. Save & Finish promotes the draft; the event rows are
    untouched by finalize."""
    from spinlab.protocol import LevelEntranceEvent, LevelExitEvent
    run_id = started_session.recorder.capture_run_id
    # Drive one clean segment through the recorder.
    started_session.recorder.handle_entrance(
        LevelEntranceEvent(level=1, timestamp_ms=0, state_path="/s.mss"),
    )
    started_session.recorder.handle_exit(
        LevelExitEvent(level=1, goal="normal", timestamp_ms=1500), "smw",
    )

    # One survived event in attempts for this run, with raw wall-clock.
    event_count = db.conn.execute(
        "SELECT COUNT(*) FROM attempts WHERE capture_run_id = ?", (run_id,),
    ).fetchone()[0]
    assert event_count == 1

    result = await started_session.save_and_finish_run(Mode.REFERENCE, name="My Run")
    assert result.status == Status.OK
    assert result.new_mode == Mode.IDLE
    row = db.conn.execute("SELECT status, name FROM capture_runs WHERE id = ?", (run_id,)).fetchone()
    assert row[0] == "saved"
    assert row[1] == "My Run"

    # Event row still there post-finalize — finalize does not touch attempts.
    event_count_after = db.conn.execute(
        "SELECT COUNT(*) FROM attempts WHERE capture_run_id = ?", (run_id,),
    ).fetchone()[0]
    assert event_count_after == 1
```

(c) `test_discard_run_hard_deletes_everything` — drop the `db.add_recorded_segment_time` call (the test's intent is "discard hard-deletes the run row and its sessions", which doesn't require timing data):

```python
@pytest.mark.asyncio
async def test_discard_run_hard_deletes_everything(started_session, db):
    run_id = started_session.recorder.capture_run_id
    await started_session.stop_reference(Mode.REFERENCE)

    result = await started_session.discard_run()
    assert result.status == Status.OK
    assert started_session.paused_run_id is None
    assert db.list_capture_sessions_for_run(run_id) == []
    rows = db.conn.execute("SELECT COUNT(*) FROM capture_runs WHERE id = ?", (run_id,)).fetchone()
    assert rows[0] == 0
```

(d) `test_save_and_finish_is_atomic_rolls_back_on_failure` — the original test exercised the seed path by inserting a `recorded_segment_times` row pointing at a non-existent segment_id, which triggered an FK error in the seed-INSERT. After the refactor the seed path is gone. Rewrite to inject a failure inside the new finalize transaction:

```python
@pytest.mark.asyncio
async def test_save_and_finish_is_atomic_rolls_back_on_failure(started_session, db, monkeypatch):
    """save_and_finish_run rolls back if any mutation in the atomic block raises.

    Choice of fault: monkeypatch db.conn so the activation step (UPDATE
    capture_runs SET active=1) raises. All prior mutations — end_capture_session,
    promote_draft — must roll back. The run remains draft and the session is
    not ended.
    """
    run_id = started_session.recorder.capture_run_id
    sess_id = started_session.recorder.current_capture_session_id
    real_conn = db.conn

    class FailingConn:
        def execute(self, sql, *args, **kwargs):
            if "SET active = 1" in sql or "SET active=1" in sql:
                raise RuntimeError("injected failure mid-transaction")
            return real_conn.execute(sql, *args, **kwargs)
        def commit(self): return real_conn.commit()
        def rollback(self): return real_conn.rollback()
        @property
        def in_transaction(self): return real_conn.in_transaction

    monkeypatch.setattr(db, "conn", FailingConn())

    with pytest.raises(RuntimeError, match="injected failure"):
        await started_session.save_and_finish_run(Mode.REFERENCE, name="Should Roll Back")

    monkeypatch.undo()

    row = db.conn.execute("SELECT status FROM capture_runs WHERE id = ?", (run_id,)).fetchone()
    assert row is not None and row[0] == "draft", "run must remain draft after rollback"
    sess_row = db.conn.execute(
        "SELECT ended_at FROM capture_sessions WHERE id = ?", (sess_id,),
    ).fetchone()
    assert sess_row[0] is None, "session-end must be rolled back"
```

The remaining tests in this file (the recovery / list / stranded-drafts / session-ordinal / etc. tests) do not touch `recorded_segment_times` and need no changes.

- [ ] **Step 2: Run the multi-session tests to verify green**

Run: `python -m pytest tests/unit/capture/test_multi_session.py -v`
Expected: All tests PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/unit/capture/test_multi_session.py
git commit -m "$(cat <<'EOF'
tests: update multi-session tests for event-level recorder

Tests that previously called db.add_recorded_segment_time(...) and
then asserted finalize seeded attempts are rewritten to either drive
a real segment through the recorder (producing event rows directly)
or drop the recorded-times setup entirely where the test's actual
intent doesn't need timing data.

The atomicity test now injects its failure on the activation step
(no seed path left to fault).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Update crash-recovery test to use attempts rows

**Files:**
- Modify: `tests/integration/test_crash_recovery.py`

The crash-recovery test currently asserts that `recorded_segment_times` rows persist across a simulated dashboard crash. After the refactor, the same crash-safety claim is made against `attempts` rows produced by the recorder.

- [ ] **Step 1: Rewrite `test_dashboard_crash_mid_session_recovers`**

Replace its body in `tests/integration/test_crash_recovery.py`:

```python
@pytest.mark.asyncio
async def test_dashboard_crash_mid_session_recovers(db, db_path, tmp_path):
    """Crash mid-recording: a closed segment's event rows survive on
    disk; on restart the paused run is recovered and resume creates a
    new session ordinal+1."""
    from spinlab.protocol import LevelEntranceEvent, LevelExitEvent

    # --- Pre-crash: start a run, close one segment via the recorder, die
    #     without graceful shutdown. The closed segment's event row must
    #     land in `attempts` (durable because _close_segment commits).
    emu = FakeEmuBackend(connected=True)
    controller = ReferenceController(db, emu)
    await controller.start_reference(Mode.IDLE, "smw", tmp_path, run_name="Long Run")
    run_id = controller.recorder.capture_run_id
    sess_id_1 = controller.recorder.current_capture_session_id

    controller.recorder.handle_entrance(
        LevelEntranceEvent(level=1, timestamp_ms=0, state_path=None),
    )
    controller.recorder.handle_exit(
        LevelExitEvent(level=1, goal="normal", timestamp_ms=1000), "smw",
    )

    # Simulate crash: drop the controller and DB references without ending
    # the session or finalizing the run.
    del controller
    db.close()

    # --- Post-crash: new dashboard instance, same DB file ---
    db2 = Database(str(db_path))
    tcp2 = FakeEmuBackend(connected=True)
    controller2 = ReferenceController(db2, tcp2)
    controller2.recover_paused_run("smw")

    assert controller2.paused_run_id == run_id
    sessions = db2.list_capture_sessions_for_run(run_id)
    assert len(sessions) == 1
    assert sessions[0]["end_reason"] == "crashed"

    # The closed segment's event row survived the crash.
    events = db2.conn.execute(
        "SELECT outcome, time_ms FROM attempts WHERE capture_run_id = ?",
        (run_id,),
    ).fetchall()
    assert [(r[0], r[1]) for r in events] == [("survived", 1000)]

    # --- Resume creates session 2 ---
    await controller2.resume_reference(Mode.IDLE, "smw", tmp_path)
    sess_id_2 = controller2.recorder.current_capture_session_id
    assert sess_id_2 != sess_id_1
    sessions = db2.list_capture_sessions_for_run(run_id)
    assert [s["ordinal"] for s in sessions] == [1, 2]
    db2.close()
```

The other test in this file (`test_paused_run_survives_replay_then_dashboard_restart`) does not touch `recorded_segment_times` and needs no changes.

- [ ] **Step 2: Run the crash-recovery test to verify green**

Run: `python -m pytest tests/integration/test_crash_recovery.py -v`
Expected: Both tests PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_crash_recovery.py
git commit -m "$(cat <<'EOF'
tests: crash-recovery now asserts attempts rows survived

Closed-segment events written via SegmentRecorder._close_segment are
durable across a simulated dashboard crash. Test now reads attempts
instead of recorded_segment_times to verify the same crash-safety
bound (completed segments durable; in-flight segment lost).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: End-to-end integration test for reference recording with deaths

**Files:**
- Create: `tests/integration/test_reference_event_recording.py`

A new integration test that drives a full reference session through `ReferenceController` (entrance → deaths → spawn → checkpoint → death → spawn → goal) and asserts the expected shape in `attempts`. This is the "did Andrew's actual ask get solved" gate.

- [ ] **Step 1: Write the integration test**

Create `tests/integration/test_reference_event_recording.py`:

```python
"""Reference recording with deaths produces real per-event attempt rows.

Drives a full reference session through ReferenceController with deaths
in each segment, then asserts the events in `attempts` reflect actual
wall-clock deltas (not synthesized penalty math).

This is the end-to-end gate for the segments-v07 reference-event-level
refactor. Pure Python — no live emulator, no network I/O.
"""
import pytest
from tests.conftest import FakeEmuBackend

from spinlab.capture import ReferenceController
from spinlab.db import Database
from spinlab.models import Mode
from spinlab.protocol import (
    CheckpointEvent,
    DeathEvent,
    LevelEntranceEvent,
    LevelExitEvent,
    SpawnEvent,
)

# Override the module-wide emulator mark from tests/integration/conftest.py.
pytestmark = []


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "ref.db"))
    d.upsert_game("smw", "Super Mario World", "any%")
    yield d
    d.close()


@pytest.mark.asyncio
async def test_reference_run_with_deaths_writes_event_rows(db, tmp_path):
    """Reference run with 1 death before cp1 and 2 deaths before goal.
    Expected event rows per segment:
      seg1 (entrance→cp1):  died (t=2000-1000=1000), survived (t=4500-2000=2500)
      seg2 (cp1→goal):      died, died, survived — all sharing an episode_id
                            distinct from seg1's.
    """
    emu = FakeEmuBackend(connected=True)
    ctl = ReferenceController(db, emu)
    await ctl.start_reference(Mode.IDLE, "smw", tmp_path, run_name="Death-Run")
    run_id = ctl.recorder.capture_run_id
    assert run_id is not None

    # --- Segment 1: entrance → cp1, with one death ---
    await ctl.handle_entrance(LevelEntranceEvent(
        level=1, state_path=None, timestamp_ms=1000, conditions={},
    ))
    ctl.handle_death(DeathEvent())  # buffered: died, time=1000 (2000-1000)
    # Hand the recorder the death timestamp so its delta math is honest.
    # In production, DeathEvent.timestamp_ms is set by the detector; the
    # FakeEmuBackend path doesn't set it, so prime _last_event_ms via the
    # death helper directly:
    ctl.recorder.handle_death(timestamp_ms=2000)
    ctl.handle_spawn(SpawnEvent(
        level_num=1, state_path=None, is_cold_cp=False, cp_ordinal=None,
        timestamp_ms=3000, conditions={},
    ), game_id="smw")
    await ctl.handle_checkpoint(CheckpointEvent(
        level_num=1, cp_ordinal=1, timestamp_ms=4500, conditions={},
    ), game_id="smw")

    # --- Segment 2: cp1 → goal, with two deaths ---
    ctl.handle_death(DeathEvent())
    ctl.recorder.handle_death(timestamp_ms=5500)  # delta = 5500-4500 = 1000
    ctl.handle_spawn(SpawnEvent(
        level_num=1, state_path=None, is_cold_cp=False, cp_ordinal=None,
        timestamp_ms=6000, conditions={},
    ), game_id="smw")
    ctl.handle_death(DeathEvent())
    ctl.recorder.handle_death(timestamp_ms=7000)  # delta = 7000-5500 = 1500
    ctl.handle_spawn(SpawnEvent(
        level_num=1, state_path=None, is_cold_cp=False, cp_ordinal=None,
        timestamp_ms=7500, conditions={},
    ), game_id="smw")
    ctl.handle_exit(LevelExitEvent(
        level=1, goal="normal", timestamp_ms=9000, conditions={},
    ), game_id="smw")

    # All event rows for this run, in order.
    rows = db.conn.execute(
        "SELECT segment_id, episode_id, outcome, time_ms, source "
        "FROM attempts WHERE capture_run_id = ? ORDER BY id", (run_id,),
    ).fetchall()

    # Expected: 5 events total (1 died + 1 survived for seg1;
    #                            2 died + 1 survived for seg2).
    assert len(rows) == 5
    seg_ids = {r["segment_id"] for r in rows}
    assert len(seg_ids) == 2

    # Group by segment_id and check shape per segment.
    by_seg: dict[str, list[dict]] = {}
    for r in rows:
        by_seg.setdefault(r["segment_id"], []).append(dict(r))

    # Each segment's events share one episode_id; episode_ids differ across segments.
    ep_ids = []
    for seg_id, events in by_seg.items():
        eids = {e["episode_id"] for e in events}
        assert len(eids) == 1, f"segment {seg_id} events span multiple episodes"
        ep_ids.append(next(iter(eids)))
    assert len(set(ep_ids)) == 2, "episode_ids must differ across segments"

    # Verify per-segment outcomes and time deltas. Identify segments by the
    # number of events (seg1 has 2, seg2 has 3).
    seg1_events = next(es for es in by_seg.values() if len(es) == 2)
    seg2_events = next(es for es in by_seg.values() if len(es) == 3)

    assert [e["outcome"] for e in seg1_events] == ["died", "survived"]
    assert [e["time_ms"] for e in seg1_events] == [1000, 2500]
    # All seg1 events should be source='reference'.
    assert all(e["source"] == "reference" for e in seg1_events)

    assert [e["outcome"] for e in seg2_events] == ["died", "died", "survived"]
    assert [e["time_ms"] for e in seg2_events] == [1000, 1500, 1500]
    assert all(e["source"] == "reference" for e in seg2_events)
```

- [ ] **Step 2: Run the new test to verify green**

Run: `python -m pytest tests/integration/test_reference_event_recording.py -v`
Expected: PASS.

If it fails because `DeathEvent` carries a `timestamp_ms` field, the `ctl.handle_death(DeathEvent())` then `ctl.recorder.handle_death(timestamp_ms=...)` dual-call workaround documented in the test is the right pattern; the production code path sets `timestamp_ms` via the detector, but `FakeEmuBackend` doesn't drive that.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_reference_event_recording.py
git commit -m "$(cat <<'EOF'
tests: integration test for reference run with deaths

End-to-end gate: drive a reference session through ReferenceController
with deaths in each segment, assert the expected per-event rows land
in attempts with real wall-clock deltas and distinct per-segment
episode_ids. This is the actual ask that motivated the v07 refactor.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Drop the `recorded_segment_times` table

**Files:**
- Create: `python/spinlab/db/migrations/0004_drop_recorded_segment_times.sql`
- Modify: `python/spinlab/db/__init__.py`
- Delete: `python/spinlab/db/recorded_segment_times.py`
- Delete: `tests/unit/db/test_db_recorded_segment_times.py`

The table is no longer written to or read from. Drop it via migration, remove the mixin from the Database composition, and delete the now-unused source/test files.

- [ ] **Step 1: Confirm no remaining production references to the table or mixin**

Run: `python -c "import subprocess; print(subprocess.check_output(['grep', '-rn', 'recorded_segment_times\\|RecordedSegmentTimes', 'python/spinlab/'], text=True))"`
Expected: Only matches in `python/spinlab/db/__init__.py` (the mixin import + composition entry) and `python/spinlab/db/recorded_segment_times.py` (the module to be deleted). No matches in capture/ or anywhere else.

If any other matches appear, stop and reconcile — they would break at runtime once the table is gone.

- [ ] **Step 2: Create migration 0004**

Create `python/spinlab/db/migrations/0004_drop_recorded_segment_times.sql`:

```sql
-- segments-v07 reference-event-level refactor: the SegmentRecorder now writes
-- per-event rows directly to `attempts` at segment close. The old
-- `recorded_segment_times` table existed only as a buffer that the finalize
-- path drained into seed-attempts; with the event-level recorder the buffer
-- has no readers left.
--
-- Forward-only drop. No data migration: the previous reference data shape
-- (one summary row per segment) is not recoverable into the new event-level
-- shape without per-event timestamps that were never captured.

DROP TABLE IF EXISTS recorded_segment_times;
```

- [ ] **Step 3: Update `python/spinlab/db/__init__.py` to drop the mixin**

```python
"""SpinLab database layer — SQLite.

The Database class composes focused repository mixins so that query logic
is organized by domain while consumers see a single object.
"""

from .attempts import AttemptsMixin
from .capture_runs import CaptureRunsMixin
from .capture_sessions import CaptureSessionsMixin
from .core import DatabaseCore
from .model_state import ModelStateMixin
from .segment_fits import SegmentFitsMixin
from .segments import SegmentsMixin
from .sessions import SessionsMixin
from .waypoints import WaypointsMixin


class Database(
    WaypointsMixin,
    SegmentsMixin,
    AttemptsMixin,
    SessionsMixin,
    ModelStateMixin,
    CaptureRunsMixin,
    CaptureSessionsMixin,
    SegmentFitsMixin,
    DatabaseCore,
):
    """Unified database interface composed from domain-specific mixins."""
    pass
```

- [ ] **Step 4: Delete the now-unused files**

```bash
git rm python/spinlab/db/recorded_segment_times.py tests/unit/db/test_db_recorded_segment_times.py
```

- [ ] **Step 5: Run full pytest to confirm green**

Run: `python -m pytest 2>&1 | tail -10`
Expected: All tests PASS. The migration will run automatically on the `:memory:` databases test fixtures create, dropping the table on each fresh connection. Existing on-disk DBs (data/spinlab.db) will have the table dropped on the next `Database()` open.

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/db/migrations/0004_drop_recorded_segment_times.sql python/spinlab/db/__init__.py python/spinlab/db/recorded_segment_times.py tests/unit/db/test_db_recorded_segment_times.py
git commit -m "$(cat <<'EOF'
db: drop recorded_segment_times table (no readers left)

Migration 0004 drops the table. RecordedSegmentTimesMixin removed
from the Database composition. The recorded_segment_times.py module
and its dedicated tests are deleted.

Reference recording now writes per-event rows directly to `attempts`
at segment close; the buffer-and-drain path is gone.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Final verification

- [ ] **Step 1: Full pytest**

Run: `python -m pytest`
Expected: All tests PASS, no skips beyond the normal emulator-test skips. Same warning count as baseline.

- [ ] **Step 2: Live dashboard sanity check (optional, requires emulator)**

If you have access to RetroArch and the test ROM, do a quick reference run with one intentional death to confirm the inventory shows `died` events:

```powershell
spinlab dashboard --config config.yaml
# (run a reference, die once, finalize)
spinlab fit inventory --game <id>
```

Expected: `Event attempts: N across M segments` with both `survived` and `died` populated; source `reference` shows >0 events.

This is not a test gate — it's a human sanity check that the production path produces the expected shape. The integration test in Task 5 already exercises the same code paths through the controller API.
