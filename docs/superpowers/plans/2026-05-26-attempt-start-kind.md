# Attempt Start-Kind (Cold/Hot) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an `is_hot` column to `attempts` so cold/hot starts can be distinguished per row, with backfill that infers hot starts from existing reference-run data and live tagging from the recorder going forward.

**Architecture:** New nullable-default column on the `attempts` table (default cold, since cold dominates). The reference recorder is the only source that emits `is_hot=1`, and only when the next episode is armed via a checkpoint (player carried state from a completed prior segment). Practice and hyper-play stay cold. Historical reference data gets a SQL backfill that walks each capture_run's attempt chain to mark first-event-of-episode rows as hot where the immediately prior attempt in the same run survived.

**Tech Stack:** SQLite (migration 0007), Python 3.11 dataclasses, the existing `migrations/` runner and `log_event_attempt` writer.

---

## File Structure

**New files:**
- `python/spinlab/db/migrations/0007_attempt_start_kind.sql` — ADD COLUMN + REFERENCE backfill
- `tests/unit/db/test_attempt_start_kind.py` — migration + backfill behavior tests
- `tests/unit/capture/test_recorder_start_kind.py` — recorder marks hot correctly on checkpoint-armed episodes

**Modified files:**
- `python/spinlab/models.py` — add `is_hot: bool = False` to `EventAttempt`
- `python/spinlab/db/attempts.py` — `EventAttemptRow` TypedDict + `log_event_attempt` INSERT + read mapper
- `python/spinlab/capture/recorder.py` — `_PendingEvent` carries `is_hot`; first event of cp-armed episode is hot
- `python/spinlab/practice.py` — explicit `is_hot=False` on the practice `EventAttempt`
- `docs/GLOSSARY.md` — define cold/hot at the attempt level

---

## Task 1: Migration — add column and backfill

**Files:**
- Create: `python/spinlab/db/migrations/0007_attempt_start_kind.sql`
- Create: `tests/unit/db/test_attempt_start_kind.py`

- [ ] **Step 1: Write the failing migration tests**

```python
# tests/unit/db/test_attempt_start_kind.py
"""Migration 0007 — is_hot column + reference-run backfill."""
from __future__ import annotations

import sqlite3

import pytest

from spinlab.db import Database


def _columns(db: Database, table: str) -> set[str]:
    return {r["name"] for r in db.conn.execute(f"PRAGMA table_info({table})").fetchall()}


def test_column_added_with_cold_default():
    db = Database(":memory:")
    assert "is_hot" in _columns(db, "attempts")
    # Insert a row without specifying is_hot; default must be 0 (cold).
    db.upsert_game("g1", "G", "any%")
    db.create_session("s1", "g1")
    from spinlab.models import Segment
    db.upsert_segment(Segment(
        id="seg1", game_id="g1", level_number=1,
        start_type="entrance", start_ordinal=0,
        end_type="goal", end_ordinal=0, description="L1",
    ))
    db.conn.execute(
        "INSERT INTO attempts (segment_id, session_id, episode_id, outcome, "
        "time_ms, source, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("seg1", "s1", "ep1", "survived", 1000, "practice", "2026-05-26T00:00:00"),
    )
    row = db.conn.execute("SELECT is_hot FROM attempts WHERE episode_id = 'ep1'").fetchone()
    assert row["is_hot"] == 0


def _insert_attempt(
    db: Database, *, segment_id: str, capture_run_id: str | None = None,
    session_id: str | None = None, episode_id: str, outcome: str,
    source: str = "reference", created_at: str = "2026-05-26T00:00:00",
) -> int:
    cur = db.conn.execute(
        "INSERT INTO attempts (segment_id, session_id, capture_run_id, episode_id, "
        "outcome, time_ms, source, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (segment_id, session_id, capture_run_id, episode_id, outcome, 1000, source, created_at),
    )
    return cur.lastrowid  # type: ignore[return-value]


def _seed_run(db: Database) -> str:
    """Create a capture_run + two segments for use in backfill tests."""
    from spinlab.models import Segment
    db.upsert_game("g1", "G", "any%")
    db.conn.execute(
        "INSERT INTO capture_runs (id, game_id, draft, created_at) VALUES (?, ?, 1, ?)",
        ("run1", "g1", "2026-05-26T00:00:00"),
    )
    for sid in ("segA", "segB"):
        db.upsert_segment(Segment(
            id=sid, game_id="g1", level_number=1,
            start_type="entrance", start_ordinal=0,
            end_type="goal", end_ordinal=0, description=sid,
        ))
    return "run1"


def test_backfill_hot_when_prior_attempt_survived_different_episode():
    """A reference attempt that is the first of its episode is HOT iff the
    immediately prior attempt in the same capture_run survived and was from
    a different episode."""
    db = Database(":memory:")
    run_id = _seed_run(db)
    # segA episode completes (survived) → next attempt on segB is hot.
    a1 = _insert_attempt(db, segment_id="segA", capture_run_id=run_id,
                         episode_id="epA", outcome="survived")
    a2 = _insert_attempt(db, segment_id="segB", capture_run_id=run_id,
                         episode_id="epB", outcome="survived")
    # Need to re-run the migration's backfill on data we just inserted. The
    # migration ran at Database() init, before our seed. Manually trigger
    # the same UPDATE the migration runs to verify the SQL on populated data.
    from pathlib import Path
    sql = Path("python/spinlab/db/migrations/0007_attempt_start_kind.sql").read_text()
    backfill = sql.split("-- BACKFILL")[1]  # everything after the marker
    db.conn.executescript(backfill)
    db.conn.commit()

    row_a1 = db.conn.execute("SELECT is_hot FROM attempts WHERE id = ?", (a1,)).fetchone()
    row_a2 = db.conn.execute("SELECT is_hot FROM attempts WHERE id = ?", (a2,)).fetchone()
    assert row_a1["is_hot"] == 0, "first attempt of run is cold (level start)"
    assert row_a2["is_hot"] == 1, "first attempt of new segment after survival is hot"


def test_backfill_cold_when_prior_attempt_died():
    """If the immediately prior attempt in the same run was a death, the
    next first-of-episode is COLD (post-death respawn)."""
    db = Database(":memory:")
    run_id = _seed_run(db)
    a1 = _insert_attempt(db, segment_id="segA", capture_run_id=run_id,
                         episode_id="epA", outcome="died")
    a2 = _insert_attempt(db, segment_id="segA", capture_run_id=run_id,
                         episode_id="epA2", outcome="survived")

    from pathlib import Path
    sql = Path("python/spinlab/db/migrations/0007_attempt_start_kind.sql").read_text()
    backfill = sql.split("-- BACKFILL")[1]
    db.conn.executescript(backfill)
    db.conn.commit()

    row_a2 = db.conn.execute("SELECT is_hot FROM attempts WHERE id = ?", (a2,)).fetchone()
    assert row_a2["is_hot"] == 0


def test_backfill_cold_for_practice_attempts():
    """Practice attempts are always cold even after a prior survival."""
    db = Database(":memory:")
    db.upsert_game("g1", "G", "any%")
    db.create_session("sess1", "g1")
    from spinlab.models import Segment
    db.upsert_segment(Segment(
        id="seg1", game_id="g1", level_number=1,
        start_type="entrance", start_ordinal=0,
        end_type="goal", end_ordinal=0, description="L1",
    ))
    a1 = _insert_attempt(db, segment_id="seg1", session_id="sess1",
                         episode_id="ep1", outcome="survived", source="practice")
    a2 = _insert_attempt(db, segment_id="seg1", session_id="sess1",
                         episode_id="ep2", outcome="survived", source="practice")

    from pathlib import Path
    sql = Path("python/spinlab/db/migrations/0007_attempt_start_kind.sql").read_text()
    backfill = sql.split("-- BACKFILL")[1]
    db.conn.executescript(backfill)
    db.conn.commit()

    for aid in (a1, a2):
        row = db.conn.execute("SELECT is_hot FROM attempts WHERE id = ?", (aid,)).fetchone()
        assert row["is_hot"] == 0


def test_backfill_subsequent_attempts_in_same_episode_are_cold():
    """Within one episode, the first attempt may be hot but all post-death
    respawns are cold."""
    db = Database(":memory:")
    run_id = _seed_run(db)
    # segA completes, then segB episode has multiple lives (die, die, survive).
    _insert_attempt(db, segment_id="segA", capture_run_id=run_id,
                    episode_id="epA", outcome="survived")
    first_b = _insert_attempt(db, segment_id="segB", capture_run_id=run_id,
                              episode_id="epB", outcome="died")
    second_b = _insert_attempt(db, segment_id="segB", capture_run_id=run_id,
                               episode_id="epB", outcome="died")
    third_b = _insert_attempt(db, segment_id="segB", capture_run_id=run_id,
                              episode_id="epB", outcome="survived")

    from pathlib import Path
    sql = Path("python/spinlab/db/migrations/0007_attempt_start_kind.sql").read_text()
    backfill = sql.split("-- BACKFILL")[1]
    db.conn.executescript(backfill)
    db.conn.commit()

    rows = {r["id"]: r["is_hot"] for r in db.conn.execute(
        "SELECT id, is_hot FROM attempts WHERE episode_id = 'epB'"
    ).fetchall()}
    assert rows[first_b] == 1, "first life of segB after surviving segA is hot"
    assert rows[second_b] == 0, "second life (post-death respawn) is cold"
    assert rows[third_b] == 0, "third life (post-death respawn) is cold"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/db/test_attempt_start_kind.py -v`
Expected: All FAIL — `is_hot` column does not exist yet.

- [ ] **Step 3: Write the migration**

```sql
-- python/spinlab/db/migrations/0007_attempt_start_kind.sql
-- Add per-attempt cold/hot start tracking.
--
-- "Cold" = spawn from a fresh load (level start, post-death respawn, practice
-- savestate load, hyper-play savestate load). The player has no carried state
-- from prior segments; powerups are whatever the load gave them.
--
-- "Hot" = spawn from carrying live state out of a completed prior segment.
-- Currently produced only by the reference recorder when a checkpoint arms
-- the next episode. Practice and hyper-play emit cold-only today; they
-- *could* gather hot data in the future (see plan
-- 2026-05-26-attempt-start-kind.md and BACKLOG).

ALTER TABLE attempts ADD COLUMN is_hot INTEGER NOT NULL DEFAULT 0;

-- BACKFILL: for REFERENCE attempts only, mark the first attempt of each
-- episode as HOT iff the *immediately preceding* attempt in the same
-- capture_run (by id, which is monotonic insertion order) was a survival
-- from a *different* episode. That signature uniquely identifies "player
-- completed a prior segment and carried state into this one."
--
-- Anything else stays cold: level starts, post-death respawns (same episode
-- as the prior died attempt), practice/hyper-play rows.

UPDATE attempts SET is_hot = 1
WHERE id IN (
  SELECT first_evt.id
  FROM (
    SELECT MIN(id) AS id, episode_id, capture_run_id
    FROM attempts
    WHERE source = 'reference'
      AND capture_run_id IS NOT NULL
    GROUP BY episode_id
  ) AS first_evt
  WHERE EXISTS (
    SELECT 1
    FROM attempts prev
    WHERE prev.capture_run_id = first_evt.capture_run_id
      AND prev.outcome = 'survived'
      AND prev.episode_id != first_evt.episode_id
      AND prev.id = (
        SELECT MAX(p2.id) FROM attempts p2
        WHERE p2.capture_run_id = first_evt.capture_run_id
          AND p2.id < first_evt.id
      )
  )
);
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/db/test_attempt_start_kind.py -v`
Expected: All 5 tests PASS.

- [ ] **Step 5: Verify full test suite still passes**

Run: `python -m pytest -m "not emulator" -x`
Expected: Same pass count as before plus the 5 new tests. Existing tests must not regress (the new column has a default so nothing existing should care).

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/db/migrations/0007_attempt_start_kind.sql tests/unit/db/test_attempt_start_kind.py
git commit -m "feat(db): add is_hot column with reference-run backfill (migration 0007)"
```

---

## Task 2: Plumb is_hot through EventAttempt and the DB writer

**Files:**
- Modify: `python/spinlab/models.py:190-220` (EventAttempt dataclass)
- Modify: `python/spinlab/db/attempts.py:68-81` (EventAttemptRow TypedDict)
- Modify: `python/spinlab/db/attempts.py:231-247` (log_event_attempt)
- Modify: `python/spinlab/db/attempts.py:382-` (get_segment_event_rows row mapping if needed)
- Test: `tests/unit/db/test_event_level_attempts.py` (extend with is_hot round-trip)

- [ ] **Step 1: Write the failing round-trip test**

Add to `tests/unit/db/test_event_level_attempts.py`:

```python
def test_event_attempt_round_trip_with_is_hot(db_with_segment: Database):
    """EventAttempt with is_hot=True persists and round-trips through
    log_event_attempt + get_segment_event_rows."""
    from spinlab.models import EventAttempt, AttemptOutcome, AttemptSource

    db_with_segment.create_capture_run("run1", "g1", "test run")
    db_with_segment.log_event_attempt(EventAttempt(
        segment_id="seg1",
        episode_id="ep1",
        outcome=AttemptOutcome.SURVIVED,
        time_ms=1000,
        capture_run_id="run1",
        source=AttemptSource.REFERENCE,
        is_hot=True,
    ))
    rows = db_with_segment.get_segment_event_rows("seg1")
    assert len(rows) == 1
    assert rows[0]["is_hot"] == 1  # SQLite returns int, not bool

def test_event_attempt_default_is_cold(db_with_segment: Database):
    """An EventAttempt with no explicit is_hot persists as cold (0)."""
    from spinlab.models import EventAttempt, AttemptOutcome, AttemptSource

    db_with_segment.log_event_attempt(EventAttempt(
        segment_id="seg1",
        episode_id="ep1",
        outcome=AttemptOutcome.SURVIVED,
        time_ms=1000,
        session_id="sess1",
        source=AttemptSource.PRACTICE,
    ))
    rows = db_with_segment.get_segment_event_rows("seg1")
    assert rows[0]["is_hot"] == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/db/test_event_level_attempts.py::test_event_attempt_round_trip_with_is_hot tests/unit/db/test_event_level_attempts.py::test_event_attempt_default_is_cold -v`
Expected: FAIL — `EventAttempt` has no `is_hot` field; `is_hot` not in returned row.

- [ ] **Step 3: Add is_hot to EventAttempt dataclass**

Edit `python/spinlab/models.py`, the `EventAttempt` dataclass (around line 190-220). Add the field after `invalidated`:

```python
@dataclass(frozen=True)
class EventAttempt:
    # ... existing fields ...
    segment_id: str
    episode_id: str
    outcome: AttemptOutcome
    time_ms: int
    session_id: str | None = None
    capture_run_id: str | None = None
    source: AttemptSource = AttemptSource.PRACTICE
    chosen_allocator: str | None = None
    invalidated: bool = False
    is_hot: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
```

- [ ] **Step 4: Update EventAttemptRow TypedDict**

Edit `python/spinlab/db/attempts.py:68-81`:

```python
class EventAttemptRow(TypedDict):
    """Raw event row, exposed for tests and the Phase 1 segments-model adapter."""
    id: int
    segment_id: str
    session_id: str | None
    capture_run_id: str | None
    episode_id: str
    outcome: str
    time_ms: int
    source: str
    chosen_allocator: str | None
    invalidated: int
    is_hot: int
    created_at: str
```

- [ ] **Step 5: Update log_event_attempt INSERT**

Edit `python/spinlab/db/attempts.py:231-247`:

```python
def log_event_attempt(self, event: EventAttempt) -> int:
    """Persist one per-event row. Primitive writer used by production."""
    cur = self.conn.execute(
        """INSERT INTO attempts
           (segment_id, session_id, capture_run_id, episode_id, outcome,
            time_ms, source, chosen_allocator, invalidated, is_hot, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            event.segment_id, event.session_id, event.capture_run_id,
            event.episode_id, event.outcome.value,
            event.time_ms,
            event.source.value if isinstance(event.source, AttemptSource) else event.source,
            event.chosen_allocator, int(event.invalidated),
            int(event.is_hot),
            event.created_at.isoformat(),
        ),
    )
    return cur.lastrowid  # type: ignore[return-value]
```

- [ ] **Step 6: Verify get_segment_event_rows returns is_hot**

`get_segment_event_rows` returns sqlite Row objects mapped via the existing reader. Since SELECT * is used (verify by reading [db/attempts.py:382-410](python/spinlab/db/attempts.py#L382-L410)) the new column comes through automatically. If the SELECT is column-explicit, add `is_hot` to the projection.

Run: `pytest tests/unit/db/test_event_level_attempts.py -v`
Expected: All tests pass, including the two new is_hot tests.

- [ ] **Step 7: Type-check**

Run: `npx pyright python/spinlab/models.py python/spinlab/db/attempts.py`
Expected: No new errors introduced (existing baseline errors are tracked separately).

- [ ] **Step 8: Commit**

```bash
git add python/spinlab/models.py python/spinlab/db/attempts.py tests/unit/db/test_event_level_attempts.py
git commit -m "feat(models): plumb is_hot through EventAttempt and DB writer"
```

---

## Task 3: Recorder emits hot for checkpoint-armed first events

**Files:**
- Modify: `python/spinlab/capture/recorder.py:48-55` (_PendingEvent dataclass)
- Modify: `python/spinlab/capture/recorder.py:109-117` (_arm_new_episode)
- Modify: `python/spinlab/capture/recorder.py:119-130` (handle_entrance)
- Modify: `python/spinlab/capture/recorder.py:223-239` (handle_checkpoint)
- Modify: `python/spinlab/capture/recorder.py:198-217` (event flush — set is_hot on first pending event)
- Modify: `python/spinlab/capture/recorder.py` — wherever death/survived events get appended (look for `_pending_events.append`)
- Create: `tests/unit/capture/test_recorder_start_kind.py`

- [ ] **Step 1: Note the event-append sites in the recorder**

There are exactly two `_PendingEvent(...)` construction sites:
- `_close_segment` at [recorder.py:158](python/spinlab/capture/recorder.py#L158) — the closing `survived_event`
- `handle_death` at [recorder.py:282](python/spinlab/capture/recorder.py#L282) — a `died` event appended directly

Each append site needs to know whether it's the FIRST event of the episode (potentially hot) or a SUBSEQUENT event (always cold). The simplest implementation: track `_next_event_is_first: bool` on the recorder, set to True on `_arm_new_episode`, set to False after the first append; combined with `_next_first_is_hot: bool` to know whether the next "first" should be hot.

- [ ] **Step 2: Write the failing recorder test**

```python
# tests/unit/capture/test_recorder_start_kind.py
"""SegmentRecorder tags the first event of each episode as hot or cold
depending on whether the episode was armed by a checkpoint (hot — player
carried state from prior segment) or an entrance (cold — level start)."""
from __future__ import annotations

import pytest

from spinlab.db import Database
from spinlab.capture.recorder import SegmentRecorder
from spinlab.condition_registry import ConditionRegistry
from spinlab.protocol import CheckpointEvent, LevelEntranceEvent, LevelExitEvent


@pytest.fixture
def recorder() -> tuple[SegmentRecorder, Database]:
    db = Database(":memory:")
    db.upsert_game("g1", "TestGame", "any%")
    db.create_capture_run("run1", "g1", "test run")
    db.conn.execute(
        "INSERT INTO capture_sessions (id, capture_run_id, ordinal, rec_path, started_at) "
        "VALUES (?, ?, 1, ?, ?)",
        ("sess1", "run1", "/dev/null", "2026-05-26T00:00:00"),
    )
    rec = SegmentRecorder(db, ConditionRegistry({}))
    rec.capture_run_id = "run1"
    rec.current_capture_session_id = "sess1"
    return rec, db


def test_first_event_after_entrance_is_cold(recorder):
    rec, db = recorder
    rec.handle_entrance(LevelEntranceEvent(
        level=1, conditions={}, state_path=None, timestamp_ms=0,
    ))
    # End at goal with no deaths.
    rec.handle_exit(LevelExitEvent(
        level=1, conditions={}, goal="goal", timestamp_ms=5000,
    ), game_id="g1")
    rows = db.conn.execute(
        "SELECT outcome, is_hot FROM attempts ORDER BY id"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["outcome"] == "survived"
    assert rows[0]["is_hot"] == 0


def test_first_event_after_checkpoint_is_hot(recorder):
    rec, db = recorder
    rec.handle_entrance(LevelEntranceEvent(
        level=1, conditions={}, state_path=None, timestamp_ms=0,
    ))
    rec.handle_checkpoint(CheckpointEvent(
        cp_ordinal=1, level_num=1, conditions={}, state_path=None, timestamp_ms=2000,
    ), game_id="g1")
    rec.handle_exit(LevelExitEvent(
        level=1, conditions={}, goal="goal", timestamp_ms=5000,
    ), game_id="g1")
    rows = db.conn.execute(
        "SELECT outcome, is_hot, episode_id FROM attempts ORDER BY id"
    ).fetchall()
    assert len(rows) == 2
    # entrance → checkpoint segment: closed at the checkpoint, cold start.
    assert rows[0]["is_hot"] == 0
    # checkpoint → goal segment: armed via handle_checkpoint, hot start.
    assert rows[1]["is_hot"] == 1


def test_post_death_events_are_cold(recorder):
    """Within one episode, the first event is hot/cold by arm-source, but
    every post-death event in that same episode is cold (respawn)."""
    rec, db = recorder
    rec.handle_entrance(LevelEntranceEvent(
        level=1, conditions={}, state_path=None, timestamp_ms=0,
    ))
    rec.handle_checkpoint(CheckpointEvent(
        cp_ordinal=1, level_num=1, conditions={}, state_path=None, timestamp_ms=1000,
    ), game_id="g1")
    # Die in the post-cp segment, then survive on retry. handle_death takes
    # a timestamp_ms int directly, not a DeathEvent.
    rec.handle_death(timestamp_ms=2000)
    rec.handle_exit(LevelExitEvent(
        level=1, conditions={}, goal="goal", timestamp_ms=4000,
    ), game_id="g1")
    # After handle_exit flushes, fetch the events for the last episode written.
    rows = db.conn.execute(
        "SELECT outcome, is_hot FROM attempts "
        "WHERE episode_id = (SELECT episode_id FROM attempts ORDER BY id DESC LIMIT 1) "
        "ORDER BY id"
    ).fetchall()
    # Two events in the post-cp episode: died (hot, first event) + survived (cold, post-death)
    assert len(rows) == 2
    assert rows[0]["outcome"] == "died"
    assert rows[0]["is_hot"] == 1, "first event of cp-armed episode is hot even if it died"
    assert rows[1]["outcome"] == "survived"
    assert rows[1]["is_hot"] == 0, "post-death respawn is cold"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/unit/capture/test_recorder_start_kind.py -v`
Expected: FAIL — recorder always writes is_hot=0 currently.

- [ ] **Step 4: Add is_hot to _PendingEvent and track first-of-episode**

In `python/spinlab/capture/recorder.py`:

```python
@dataclass
class _PendingEvent:
    outcome: AttemptOutcome
    time_ms: int
    created_at: datetime
    is_hot: bool = False
```

In `SegmentRecorder.__init__`, add:
```python
        # Whether the NEXT event to be appended is the first of its episode.
        # Set to True on _arm_new_episode; flipped to False after the first
        # event is appended. Combined with _next_first_is_hot.
        self._next_event_is_first: bool = False
        # Whether the next first-of-episode event should be hot. Set by the
        # arm-source: True from handle_checkpoint (carry-over), False from
        # handle_entrance (level start).
        self._next_first_is_hot: bool = False
```

Update `_arm_new_episode` to accept a hot flag (default False; called from handle_entrance with False and handle_checkpoint with True):

```python
def _arm_new_episode(self, start_ts_ms: int, *, is_hot: bool = False) -> None:
    """Mint a fresh episode_id for the upcoming segment and reset
    per-segment buffer/counters."""
    self._episode_id = uuid.uuid4().hex
    self._last_event_ms = start_ts_ms
    self._pending_events = []
    self._deaths_in_segment = 0
    self._last_spawn_ms = None
    self.died = False
    self._next_event_is_first = True
    self._next_first_is_hot = is_hot
```

Update `handle_checkpoint` to pass `is_hot=True` when arming the new episode (it closes the prior segment first, then arms a new one for the cp→next pass — the new one is the hot one):

```python
def handle_checkpoint(self, event: CheckpointEvent, game_id: str) -> None:
    # ... existing close logic ...
    self.pending_start = PendingStart(...)
    # New segment starts here — fresh episode for the cp→next pass. The
    # FIRST event of this episode is HOT because the player carried state
    # from the just-completed segment.
    self._arm_new_episode(event.timestamp_ms, is_hot=True)
```

`handle_entrance` keeps the default `is_hot=False`.

- [ ] **Step 5: Tag is_hot on each event append**

Find every `self._pending_events.append(_PendingEvent(...))` site in the recorder (and any inline `_PendingEvent(...)` construction in `_close_segment`, e.g. the `survived_event` build). Wrap with a helper or inline this logic:

```python
def _append_event(self, ev: _PendingEvent) -> None:
    if self._next_event_is_first:
        ev = _PendingEvent(
            outcome=ev.outcome, time_ms=ev.time_ms,
            created_at=ev.created_at, is_hot=self._next_first_is_hot,
        )
        self._next_event_is_first = False
    self._pending_events.append(ev)
```

Then replace all `self._pending_events.append(_PendingEvent(...))` calls with `self._append_event(_PendingEvent(...))`.

The `survived_event` constructed in `_close_segment` (around line 154-162) is a closing event — it goes through the same first-of-episode logic. Build it through `_append_event` or apply the same conditional manually.

- [ ] **Step 6: Propagate is_hot through the flush**

Where the recorder writes events at [recorder.py:204-213](python/spinlab/capture/recorder.py#L204-L213):

```python
for ev in events_to_write:
    self._db.log_event_attempt(EventAttempt(
        segment_id=seg_id,
        episode_id=self._episode_id,
        outcome=ev.outcome,
        time_ms=ev.time_ms,
        capture_run_id=self.capture_run_id,
        source=AttemptSource.REFERENCE,
        is_hot=ev.is_hot,
        created_at=ev.created_at,
    ))
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `pytest tests/unit/capture/test_recorder_start_kind.py tests/unit/capture/test_recorder.py -v`
Expected: New tests PASS; existing recorder tests still PASS.

- [ ] **Step 8: Run the broader suite**

Run: `python -m pytest -m "not emulator" -x`
Expected: All previously passing tests still pass; new tests added in tasks 1-3 also pass.

- [ ] **Step 9: Commit**

```bash
git add python/spinlab/capture/recorder.py tests/unit/capture/test_recorder_start_kind.py
git commit -m "feat(recorder): tag first event of cp-armed episode as is_hot"
```

---

## Task 4: Practice path is explicitly cold

**Files:**
- Modify: `python/spinlab/practice.py:183-192` (the EventAttempt construction)

The practice EventAttempt construction doesn't pass `is_hot`, so it relies on the default (False). This task makes that explicit so future readers don't wonder if it's an oversight.

- [ ] **Step 1: Make is_hot explicit in practice**

Edit `python/spinlab/practice.py:183-192`:

```python
record = EventAttempt(
    segment_id=event.segment_id,
    episode_id=event.episode_id,
    outcome=outcome,
    time_ms=event.time_ms,
    session_id=self.session_id,
    source=AttemptSource.PRACTICE,
    chosen_allocator=self._last_allocator,
    is_hot=False,  # Practice always loads from a savestate → cold spawn.
)
```

(Hyper-play currently writes via the legacy `log_attempt` path, which synthesizes event rows via `_split_episode_into_events` and uses the column default. Leaving as-is for this branch — see the BACKLOG entry below for the future hot-hyperplay work.)

- [ ] **Step 2: Run the suite**

Run: `python -m pytest -m "not emulator" -x`
Expected: No regressions.

- [ ] **Step 3: Commit**

```bash
git add python/spinlab/practice.py
git commit -m "chore(practice): make is_hot=False explicit on practice EventAttempt"
```

---

## Task 5: Glossary + backlog entry

**Files:**
- Modify: `docs/GLOSSARY.md` (add Attempt section)
- Modify: `docs/BACKLOG.md` (add future hot-collection work)

- [ ] **Step 1: Update glossary**

Add a new section before "## Save States" in `docs/GLOSSARY.md`:

```markdown
## Attempts (cold vs hot)

The `attempts` table stores one row per died-or-survived event ("life"). One
or more attempts grouped by `episode_id` make up a player's full trial of a
segment (spawn → final outcome).

- **Cold attempt** (`is_hot=0`) — spawn from a fresh load: level start,
  post-death respawn, practice savestate load, hyper-play savestate load.
  No carried state from prior segments.
- **Hot attempt** (`is_hot=1`) — spawn carrying live state out of a
  completed prior segment. Currently produced only by the reference
  recorder when a checkpoint arms the next episode; practice and hyper-play
  emit cold-only today.

Cold dominates: every post-death respawn is cold, the first attempt of a
level is cold, and all practice/hyper-play attempts are cold. Hot attempts
are the first life of each non-first segment in a reference run.

Historical data (pre-migration 0007) was backfilled by inspecting capture-run
attempt ordering; the heuristic catches the common case but a few edge cases
(paused/resumed runs) may be mis-labeled. Going forward, the recorder tags
new attempts correctly at write time.
```

Note: the existing "Hot variant" / "Cold variant" entries under "## Save States" refer to the save-state file kind, not attempt start kind. Don't conflate the two — the glossary entries are intentionally separate.

- [ ] **Step 2: Update backlog**

Add to `docs/BACKLOG.md` (under whatever section makes sense, near other modeling/data work):

```markdown
- [ ] **Hyper-play hot data collection** — Hyper-play currently emits cold-only
  attempts. To gather hot data, refactor `hyper_play._record_attempt` to
  detect carry-over from a completed prior sub-segment and tag the first
  attempt of the next sub-segment with `is_hot=True`. Mirrors the reference
  recorder's logic. Blocked on no urgent need; revisit once the cold-hot
  modeling story matures.
- [ ] **Hot↔cold transfer modeling** — Treat cold and hot attempts as
  partially-pooled populations rather than fully independent. The
  death-aware rolling estimator and future bootstrap estimator should learn
  a transfer weight from data rather than filtering one out. Future work
  once we have meaningful hot sample sizes to validate against.
```

- [ ] **Step 3: Commit**

```bash
git add docs/GLOSSARY.md docs/BACKLOG.md
git commit -m "docs: glossary + backlog entries for attempt cold/hot"
```

---

## Task 6: Full-suite verification

- [ ] **Step 1: Run the full suite (unit + emulator + frontend smoke)**

Run: `python -m pytest`
Expected: All tests pass. Per CLAUDE.md, the full suite must be green before declaring done. SKIPPED is NOT acceptable as "passing" — investigate any skips before commit.

If emulator tests skip due to missing `SPINLAB_TEST_ROM`, that's a baseline-environment issue, not a regression from this branch; flag it but don't gate on it.

- [ ] **Step 2: Type check the touched files**

Run: `npx pyright python/spinlab/models.py python/spinlab/db/attempts.py python/spinlab/capture/recorder.py python/spinlab/practice.py`
Expected: No new errors. Existing baseline errors stay.

- [ ] **Step 3: Lint**

Run: `ruff check python/spinlab/models.py python/spinlab/db/attempts.py python/spinlab/capture/recorder.py python/spinlab/practice.py python/spinlab/db/migrations/0007_attempt_start_kind.sql`
Expected: Clean (the .sql file will be skipped silently).

---

## Out of scope (deferred to later branches)

- **Bootstrap estimator** — separate branch.
- **Hazard plot visualization with cold-only filter** — separate branch; depends on this branch landing first.
- **Hyper-play hot data collection** — backlog entry above.
- **Hot↔cold transfer modeling** — backlog entry above.
- **Episode → trial rename** — dropped per discussion 2026-05-26; keep "episode" terminology in code.
