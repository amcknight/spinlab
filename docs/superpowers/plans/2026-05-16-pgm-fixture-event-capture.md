# PGM Fixture: Per-Event Capture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

## STATUS: DEFERRED / IN FLUX

This plan supports a **cross-project handoff**: SpinLab (this repo) needs to produce a fixture sqlite that a separate PGM-prototype project consumes to validate its segment model. Both sides are evolving. **Re-read the "Open design questions" section below before executing** — any of these shifting changes the plan.

**Do not execute this plan until:**
1. The PGM-prototype project confirms the canonical fixture schema below (see "Fixture Schema") is still the target shape.
2. Andrew has decided whether cold-starts-only is still the modeling scope.
3. Andrew is ready to allocate 3-8 hours of real practice across 2-4 games to populate the fixture.

---

**Goal:** Record per-event (death / survival) timestamps inside each practice attempt, then export them as a cold-starts-only fixture sqlite for offline PGM validation.

**Architecture:** Add an `attempt_events` table that captures each terminal-or-death event within an attempt with absolute time-since-attempt-start (`t_ms`). Wire `PracticeTiming` to record events as they happen and pass the list through `AttemptResultEvent` → `session_manager` → `Database.log_attempt_events`. A standalone exporter joins `attempts` + `attempt_events` + `segments`, filters to `event_index = 0` for cold-starts-only, and writes a fixture sqlite matching the canonical fixture schema (below).

**Tech Stack:** Python 3.11, sqlite, pytest, existing SpinLab DB-migration runner.

---

## Fixture Schema (Canonical)

Agreed with the PGM-prototype project (Path B, 2026-05-17). Each row is one ENDING event (death OR survival) of a single cold-start attempt-instance. `time_ms` is wall-clock ms from the start of that attempt-instance to the event, no death-penalty time included. `created_at` is unix seconds — required for time-aware model behavior (Kalman drift, fatigue, session boundaries) and cannot be recovered after the fact. `attempt_n` is NOT stored — derived at consumption time via `ROW_NUMBER() OVER (PARTITION BY game, segment ORDER BY created_at, id)`, so a retroactive insert with an earlier `created_at` lands in its true play position without renumbering surgery.

```sql
CREATE TABLE IF NOT EXISTS attempts (
    id         INTEGER PRIMARY KEY,
    game       TEXT NOT NULL,
    segment    TEXT NOT NULL,
    outcome    TEXT NOT NULL CHECK (outcome IN ('survived', 'died')),
    time_ms    INTEGER NOT NULL,
    created_at INTEGER NOT NULL DEFAULT (unixepoch())
);

CREATE INDEX IF NOT EXISTS ix_attempts_seg
    ON attempts (game, segment, created_at, id);
```

Consumer reads with `ORDER BY created_at, id`; the secondary index covers that sort.

Deliberately NOT in the schema (defer until the PGM asks):
- `attempt_group_id` (would group events from one warm-start attempt)
- `start_kind` (`segment_start` | `respawn`) — only matters once warm-starts are in scope
- `power_state` (small | mushroom) — fixture games are picked to be invariant
- `notes` / `session_id` / `segment_version`
- `'aborted'` / `'reset'` outcomes — those rows are dropped at export

---

## Open Design Questions (revisit before executing)

These shaped the plan as written. If any answer changes, the affected tasks need reworking:

- **Cold-starts-only scope.** Plan assumes the PGM only consumes the first event of each attempt. Exporter filters `event_index = 0`. If the model widens to per-respawn events, drop the filter — recording side stays the same.
- **Constant-respawn-time assumption.** PGM-side concern only; SpinLab doesn't model respawn time. Noted for cross-project alignment.
- **Power-state invariance (small vs mushroom).** Worked around by selecting fixture games where state is invariant (kaizo counter-break style). If the model needs to handle power-state changes, scope grows substantially — out of scope here.
- **Aborted / invalidated attempts.** Plan drops them at export (`WHERE invalidated = 0 AND <attempt has at least one event>`). No `'aborted'` event kind; recording side just doesn't emit one.
- **SpeedRunTiming instrumentation.** Plan covers `PracticeTiming` only. `SpeedRunTiming` (in the same file) has the same shape and could be done in parallel, but the fixture only needs practice data (`attempts.source = 'practice'`). Decision deferred.
- **Backfill of existing attempts.** Plan assumes a DB reset before fixture-capture runs (per Andrew). No backfill task. Existing attempts have aggregate `deaths` count but no per-event timestamps — they cannot be recovered.

---

## File Structure

**New files:**
- `python/spinlab/db/migrations/0002_attempt_events.sql` — migration adding the events table.
- `python/spinlab/db/attempt_events.py` — DAO mixin for inserting/querying events.
- `python/spinlab/tools/__init__.py` — package marker (if not present).
- `python/spinlab/tools/export_fixture.py` — CLI exporter.
- `tests/unit/test_attempt_events.py` — DAO + migration tests.
- `tests/unit/test_export_fixture.py` — exporter unit tests.

**Modified files:**
- `python/spinlab/protocol.py` — extend `AttemptResultEvent` with `events: tuple[AttemptEvent, ...]`; add `AttemptEvent` dataclass.
- `python/spinlab/timing.py` — `PracticeTiming` records `_event_times: list[tuple[str, int]]`; `_emit_result` builds events into `AttemptResultEvent`.
- `python/spinlab/models.py` — `Attempt` carries `events: list[AttemptEvent]` (default empty).
- `python/spinlab/session_manager.py` — `_handle_attempt_result` calls `db.log_attempt_events` after `db.log_attempt`.
- `python/spinlab/db/__init__.py` (or the central `Database` class) — mix in `AttemptEventsMixin`.
- `tests/unit/test_timing.py` — extend existing tests to assert `events` content.

---

## Task 1: Add `attempt_events` migration

**Files:**
- Create: `python/spinlab/db/migrations/0002_attempt_events.sql`
- Create: `tests/unit/test_attempt_events.py`

- [ ] **Step 1: Write the failing migration test**

```python
# tests/unit/test_attempt_events.py
import sqlite3
from spinlab.db.migrations import run_migrations


def test_attempt_events_table_exists_after_migration(tmp_path):
    db_path = tmp_path / "test.sqlite"
    conn = sqlite3.connect(db_path)
    run_migrations(conn)
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='attempt_events'"
    )
    assert cur.fetchone() is not None


def test_attempt_events_columns(tmp_path):
    db_path = tmp_path / "test.sqlite"
    conn = sqlite3.connect(db_path)
    run_migrations(conn)
    cur = conn.execute("PRAGMA table_info(attempt_events)")
    cols = {row[1]: row[2] for row in cur.fetchall()}
    assert cols == {
        "id": "INTEGER",
        "attempt_id": "INTEGER",
        "event_index": "INTEGER",
        "kind": "TEXT",
        "t_ms": "INTEGER",
    }


def test_attempt_events_unique_index_on_attempt_and_index(tmp_path):
    db_path = tmp_path / "test.sqlite"
    conn = sqlite3.connect(db_path)
    run_migrations(conn)
    # Insert a fake attempt and two events at the same index to verify UNIQUE bites.
    conn.execute("INSERT INTO games (id, name) VALUES ('g', 'g')")
    conn.execute(
        "INSERT INTO segments (id, game_id, level_number, start_type, start_ordinal, "
        "end_type, end_ordinal, conditions_json, active) "
        "VALUES ('s', 'g', 1, 'entrance', 0, 'goal', 0, '{}', 1)"
    )
    conn.execute(
        "INSERT INTO sessions (id, game_id, started_at) VALUES ('sess', 'g', '2026-05-16')"
    )
    cur = conn.execute(
        "INSERT INTO attempts (segment_id, session_id, completed, source, created_at) "
        "VALUES ('s', 'sess', 1, 'practice', '2026-05-16')"
    )
    attempt_id = cur.lastrowid
    conn.execute(
        "INSERT INTO attempt_events (attempt_id, event_index, kind, t_ms) VALUES (?, 0, 'died', 1000)",
        (attempt_id,),
    )
    try:
        conn.execute(
            "INSERT INTO attempt_events (attempt_id, event_index, kind, t_ms) VALUES (?, 0, 'died', 2000)",
            (attempt_id,),
        )
        raised = False
    except sqlite3.IntegrityError:
        raised = True
    assert raised, "UNIQUE(attempt_id, event_index) constraint missing"
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/unit/test_attempt_events.py -v`
Expected: FAIL — `attempt_events` table does not exist.

- [ ] **Step 3: Write the migration**

```sql
-- python/spinlab/db/migrations/0002_attempt_events.sql
CREATE TABLE IF NOT EXISTS attempt_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  attempt_id INTEGER NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
  event_index INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('died', 'survived')),
  t_ms INTEGER NOT NULL,
  UNIQUE (attempt_id, event_index)
);

CREATE INDEX IF NOT EXISTS idx_attempt_events_attempt ON attempt_events(attempt_id, event_index);
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_attempt_events.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```powershell
git add python/spinlab/db/migrations/0002_attempt_events.sql tests/unit/test_attempt_events.py
git commit -m "feat(db): add attempt_events table for per-event capture"
```

---

## Task 2: `AttemptEvent` protocol type + extend `AttemptResultEvent`

**Files:**
- Modify: `python/spinlab/protocol.py` (around the existing `AttemptResultEvent` definition near line 82)
- Modify: `tests/unit/test_timing.py` (extend the existing zero-deaths test)

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_timing.py`:

```python
def test_practice_result_event_includes_events_field():
    from spinlab.protocol import AttemptEvent
    clock = _Clock()
    received: list[AttemptResultEvent] = []
    pt = PracticeTiming(now_ms=clock)
    clock.now = 0
    pt.arm(
        segment_id="seg-1", end_type="goal",
        death_penalty_ms=3200, auto_advance_delay_ms=0,
        on_attempt_result=received.append,
    )
    clock.now = 5000
    pt.observe_event(LevelExitEvent(level=5, goal="normal"))
    pt.tick(now_ms=5000)
    assert len(received) == 1
    assert received[0].events == (AttemptEvent(kind="survived", t_ms=5000),)
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/unit/test_timing.py::test_practice_result_event_includes_events_field -v`
Expected: FAIL — `AttemptEvent` not importable; `AttemptResultEvent` has no `events` field.

- [ ] **Step 3: Add `AttemptEvent` and extend `AttemptResultEvent`**

In `python/spinlab/protocol.py`, near the existing `AttemptResultEvent`:

```python
from typing import Literal

EventKind = Literal["died", "survived"]


@dataclass(frozen=True)
class AttemptEvent:
    """One terminal-or-death event inside an attempt.

    t_ms is wall-clock ms from attempt start (arm), not including death
    penalty time. Cold-starts-only consumers read only the event at
    event_index = 0.
    """
    kind: EventKind = "died"
    t_ms: int = 0


@dataclass(frozen=True)
class AttemptResultEvent:
    segment_id: str = ""
    completed: bool = False
    time_ms: int | None = None
    deaths: int = 0
    clean_tail_ms: int | None = None
    events: tuple[AttemptEvent, ...] = ()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_timing.py::test_practice_result_event_includes_events_field -v`
Expected: FAIL still — `PracticeTiming` doesn't populate events yet. **This task only adds the type; Task 3 wires the data.** Mark this test as `@pytest.mark.xfail(reason="wired in Task 3")` for now, or skip running it until Task 3.

Confirm the rest of `test_timing.py` still passes:

Run: `pytest tests/unit/test_timing.py -v`
Expected: existing tests still PASS (`events` field defaults to `()` so old assertions don't break).

- [ ] **Step 5: Commit**

```powershell
git add python/spinlab/protocol.py tests/unit/test_timing.py
git commit -m "feat(protocol): add AttemptEvent + events field on AttemptResultEvent"
```

---

## Task 3: `PracticeTiming` records and emits per-event timeline

**Files:**
- Modify: `python/spinlab/timing.py` (`PracticeTiming` class, ~lines 59-220)
- Modify: `tests/unit/test_timing.py`

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_timing.py`, remove the `xfail` from the Task 2 test, and add:

```python
def test_practice_records_each_death_event():
    from spinlab.protocol import AttemptEvent
    clock = _Clock()
    received: list[AttemptResultEvent] = []
    pt = PracticeTiming(now_ms=clock)
    clock.now = 0
    pt.arm(
        segment_id="seg-1", end_type="goal",
        death_penalty_ms=3200, auto_advance_delay_ms=0,
        on_attempt_result=received.append,
    )
    clock.now = 1500
    pt.observe_event(DeathEvent())
    clock.now = 3000
    pt.observe_event(DeathEvent())
    clock.now = 8000
    pt.observe_event(LevelExitEvent(level=5, goal="normal"))
    pt.tick(now_ms=8000)
    assert len(received) == 1
    assert received[0].events == (
        AttemptEvent(kind="died", t_ms=1500),
        AttemptEvent(kind="died", t_ms=3000),
        AttemptEvent(kind="survived", t_ms=8000),
    )


def test_practice_records_died_only_when_aborted_by_level_exit():
    from spinlab.protocol import AttemptEvent
    clock = _Clock()
    received: list[AttemptResultEvent] = []
    pt = PracticeTiming(now_ms=clock)
    clock.now = 0
    pt.arm(
        segment_id="seg-1", end_type="goal",
        death_penalty_ms=3200, auto_advance_delay_ms=0,
        on_attempt_result=received.append,
    )
    clock.now = 2000
    pt.observe_event(DeathEvent())
    clock.now = 5000
    pt.observe_event(LevelExitEvent(level=5, goal="abort"))
    pt.tick(now_ms=5000)
    assert len(received) == 1
    # Abort emits an attempt with completed=False; the death is recorded but
    # there is NO trailing 'survived' event. The aborted attempt has 1 event.
    assert received[0].completed is False
    assert received[0].events == (AttemptEvent(kind="died", t_ms=2000),)
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/unit/test_timing.py -v -k events`
Expected: 3 failures (the Task 2 test + the two new ones).

- [ ] **Step 3: Wire event capture in `PracticeTiming`**

In `python/spinlab/timing.py`:

```python
# Inside PracticeTiming.__init__, alongside _deaths/_last_death_ms:
self._event_times: list[tuple[str, int]] = []  # (kind, t_ms_since_start)
```

In `observe_event`, where `DeathEvent` is handled (around line 130):

```python
if isinstance(event, DeathEvent):
    self._deaths += 1
    self._last_death_ms = self._now()
    self._event_times.append(("died", self._now() - self._start_ms))
    return
```

In `_enter_result` (or wherever the terminal event is processed; around line 166), append the terminal event **only when `completed` is True**. Aborts do not emit a `survived` event:

```python
def _enter_result(self, *, completed: bool) -> None:
    now = self._now()
    penalty = self._death_penalty_ms * self._deaths
    self._elapsed_ms = (now - self._start_ms) + penalty
    self._completed = completed
    self._result_start_ms = now
    if completed:
        self._event_times.append(("survived", now - self._start_ms))
    self._state = _PracticeState.RESULT
```

In `_emit_result` (around line 174), build the `AttemptResultEvent` with the events tuple:

```python
from spinlab.protocol import AttemptEvent  # add to existing imports

# Inside _emit_result, where AttemptResultEvent is constructed (~line 187):
events = tuple(
    AttemptEvent(kind=k, t_ms=t) for (k, t) in self._event_times
)
result = AttemptResultEvent(
    segment_id=self._segment_id,
    completed=self._completed,
    time_ms=int(math.floor(self._elapsed_ms)),
    deaths=self._deaths,
    clean_tail_ms=clean_tail_ms,
    events=events,
)
```

In `_reset` (around line 200), clear the list:

```python
self._event_times = []
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_timing.py -v`
Expected: all PASS (existing + new). If existing tests fail because event assertions creep into them, audit — the `events` field should be additive.

- [ ] **Step 5: Commit**

```powershell
git add python/spinlab/timing.py tests/unit/test_timing.py
git commit -m "feat(timing): record per-event timeline in PracticeTiming"
```

---

## Task 4: `Attempt` model carries events; `log_attempt_events` DAO

**Files:**
- Modify: `python/spinlab/models.py` (`Attempt` dataclass around line 143)
- Create: `python/spinlab/db/attempt_events.py`
- Modify: `python/spinlab/db/__init__.py` (or wherever `Database` composes mixins) to mix in the new DAO.

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_attempt_events.py`:

```python
from datetime import UTC, datetime

from spinlab.db import Database
from spinlab.models import Attempt
from spinlab.protocol import AttemptEvent


def _seed(db, *, segment_id="s", session_id="sess"):
    db.conn.execute("INSERT INTO games (id, name) VALUES ('g', 'g')")
    db.conn.execute(
        "INSERT INTO segments (id, game_id, level_number, start_type, start_ordinal, "
        "end_type, end_ordinal, conditions_json, active) "
        "VALUES (?, 'g', 1, 'entrance', 0, 'goal', 0, '{}', 1)",
        (segment_id,),
    )
    db.conn.execute(
        "INSERT INTO sessions (id, game_id, started_at) VALUES (?, 'g', '2026-05-16')",
        (session_id,),
    )


def test_log_attempt_events_writes_rows(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    _seed(db)
    attempt = Attempt(
        segment_id="s", completed=True, session_id="sess", time_ms=5000,
        deaths=1, clean_tail_ms=2000, created_at=datetime(2026, 5, 16, tzinfo=UTC),
    )
    attempt_id = db.log_attempt(attempt)
    db.log_attempt_events(attempt_id, [
        AttemptEvent(kind="died", t_ms=1500),
        AttemptEvent(kind="survived", t_ms=5000),
    ])
    rows = db.conn.execute(
        "SELECT event_index, kind, t_ms FROM attempt_events "
        "WHERE attempt_id = ? ORDER BY event_index", (attempt_id,)
    ).fetchall()
    assert rows == [(0, "died", 1500), (1, "survived", 5000)]


def test_log_attempt_events_empty_list_is_noop(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    _seed(db)
    attempt = Attempt(
        segment_id="s", completed=False, session_id="sess",
        created_at=datetime(2026, 5, 16, tzinfo=UTC),
    )
    attempt_id = db.log_attempt(attempt)
    db.log_attempt_events(attempt_id, [])
    cnt = db.conn.execute(
        "SELECT COUNT(*) FROM attempt_events WHERE attempt_id = ?", (attempt_id,)
    ).fetchone()[0]
    assert cnt == 0
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/unit/test_attempt_events.py::test_log_attempt_events_writes_rows -v`
Expected: FAIL — `Database.log_attempt_events` does not exist.

- [ ] **Step 3: Add `Attempt.events` field**

In `python/spinlab/models.py`, extend the `Attempt` dataclass:

```python
# Add to imports at top:
from .protocol import AttemptEvent

@dataclass
class Attempt:
    # ... existing fields ...
    events: list[AttemptEvent] = field(default_factory=list)
```

- [ ] **Step 4: Implement `AttemptEventsMixin`**

```python
# python/spinlab/db/attempt_events.py
"""Per-event capture for attempts."""

import sqlite3
from collections.abc import Sequence

from ..protocol import AttemptEvent


class AttemptEventsMixin:
    """Insert/query per-attempt event rows."""
    conn: sqlite3.Connection

    def log_attempt_events(
        self, attempt_id: int, events: Sequence[AttemptEvent],
    ) -> None:
        """Insert one row per event with stable event_index ordering.

        No-op if the events list is empty. event_index starts at 0 and
        matches the order of the supplied sequence.
        """
        if not events:
            return
        self.conn.executemany(
            "INSERT INTO attempt_events (attempt_id, event_index, kind, t_ms) "
            "VALUES (?, ?, ?, ?)",
            [(attempt_id, i, e.kind, e.t_ms) for i, e in enumerate(events)],
        )
```

- [ ] **Step 5: Mix into `Database`**

In `python/spinlab/db/__init__.py` (or wherever `Database` is defined), add `AttemptEventsMixin` to the class bases. The exact line edit depends on the file layout — find the existing mixin list (e.g., `class Database(AttemptsMixin, ...):`) and add `AttemptEventsMixin`.

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/unit/test_attempt_events.py -v`
Expected: 5 passed (3 migration + 2 DAO).

- [ ] **Step 7: Commit**

```powershell
git add python/spinlab/models.py python/spinlab/db/attempt_events.py python/spinlab/db/__init__.py tests/unit/test_attempt_events.py
git commit -m "feat(db): log_attempt_events DAO + Attempt.events field"
```

---

## Task 5: `session_manager` wires events from `AttemptResultEvent` to DB

**Files:**
- Modify: `python/spinlab/session_manager.py` (`_handle_attempt_result` or whatever turns `AttemptResultEvent` into the `Attempt` row).

- [ ] **Step 1: Locate and read the existing handler**

Run: `grep -n "_handle_attempt_result\|log_attempt" python/spinlab/session_manager.py python/spinlab/practice.py`

Identify the call site where the practice session converts an `AttemptResultEvent` into an `Attempt` and calls `db.log_attempt`. (Currently in `practice.py` around the `process_attempt` / event handler — see [practice.py:249-264](python/spinlab/practice.py#L249-L264).)

- [ ] **Step 2: Write the failing integration test**

Create or extend a practice-session test that:
1. Drives a `PracticeTiming` through arm → death → death → goal.
2. Forwards the resulting `AttemptResultEvent` to the practice session handler.
3. Asserts the DB has one row in `attempts` and N rows in `attempt_events` with the right ordering.

Skeleton:

```python
def test_practice_session_persists_events(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    _seed(db)  # from test_attempt_events.py — extract to a shared helper
    session = build_practice_session(db, ...)  # mirror existing test pattern
    result = AttemptResultEvent(
        segment_id="s", completed=True, time_ms=5000, deaths=1,
        clean_tail_ms=2000,
        events=(
            AttemptEvent(kind="died", t_ms=1500),
            AttemptEvent(kind="survived", t_ms=5000),
        ),
    )
    session.handle_attempt_result(result)
    row = db.conn.execute("SELECT id FROM attempts").fetchone()
    events = db.conn.execute(
        "SELECT event_index, kind, t_ms FROM attempt_events "
        "WHERE attempt_id = ? ORDER BY event_index", (row[0],)
    ).fetchall()
    assert events == [(0, "died", 1500), (1, "survived", 5000)]
```

- [ ] **Step 3: Run to verify it fails**

Expected: FAIL — `attempt_events` table is empty because the handler doesn't write events yet.

- [ ] **Step 4: Wire the call**

In the handler, after `attempt_id = db.log_attempt(attempt)`:

```python
db.log_attempt_events(attempt_id, list(result.events))
```

Also: ensure the `Attempt` constructed from `result` either copies `result.events` into `attempt.events` (if the DAO uses it from the model) OR is bypassed (the explicit `db.log_attempt_events` call is enough). Pick one path; don't do both.

- [ ] **Step 5: Run tests**

Run: `python -m pytest tests/unit -v` (fast suite).
Expected: all PASS.

Run: `python -m pytest -m emulator` (emulator suite — RA-dependent).
Expected: all PASS or known-skipped per [feedback_run_emulator_tests.md](../../../C:/Users/thedo/.claude/projects/c--Users-thedo-git-spinlab/memory/feedback_run_emulator_tests.md). Investigate any new failures — likely from `result.events` being unset on legacy code paths.

- [ ] **Step 6: Commit**

```powershell
git add python/spinlab/session_manager.py python/spinlab/practice.py tests/
git commit -m "feat(practice): persist attempt events on result"
```

---

## Task 6: Fixture exporter CLI

**Files:**
- Create: `python/spinlab/tools/__init__.py` (if missing)
- Create: `python/spinlab/tools/export_fixture.py`
- Create: `tests/unit/test_export_fixture.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_export_fixture.py
import sqlite3
from datetime import UTC, datetime

from spinlab.db import Database
from spinlab.models import Attempt
from spinlab.protocol import AttemptEvent
from spinlab.tools.export_fixture import export_to_fixture_sqlite


def _build_db(tmp_path):
    db = Database(tmp_path / "spinlab.sqlite")
    db.conn.execute("INSERT INTO games (id, name) VALUES ('smw', 'Super Mario World')")
    db.conn.execute(
        "INSERT INTO segments (id, game_id, level_number, start_type, start_ordinal, "
        "end_type, end_ordinal, conditions_json, active, description) "
        "VALUES ('seg-1-1', 'smw', 1, 'entrance', 0, 'goal', 0, '{}', 1, 'YI1')"
    )
    db.conn.execute(
        "INSERT INTO sessions (id, game_id, started_at) VALUES ('sess', 'smw', '2026-05-16')"
    )
    return db


def test_export_cold_starts_only_writes_first_event_per_attempt(tmp_path):
    db = _build_db(tmp_path)
    # Attempt 1: died at 1500 (then died again, then survived — only first event exported).
    a1 = db.log_attempt(Attempt(
        segment_id="seg-1-1", session_id="sess", completed=True, time_ms=8000,
        deaths=2, source="practice", created_at=datetime(2026, 5, 16, tzinfo=UTC),
    ))
    db.log_attempt_events(a1, [
        AttemptEvent(kind="died", t_ms=1500),
        AttemptEvent(kind="died", t_ms=3000),
        AttemptEvent(kind="survived", t_ms=8000),
    ])
    # Attempt 2: clean run, survived at 6000.
    a2 = db.log_attempt(Attempt(
        segment_id="seg-1-1", session_id="sess", completed=True, time_ms=6000,
        deaths=0, source="practice", created_at=datetime(2026, 5, 16, tzinfo=UTC),
    ))
    db.log_attempt_events(a2, [AttemptEvent(kind="survived", t_ms=6000)])

    out_path = tmp_path / "fixture.sqlite"
    n = export_to_fixture_sqlite(db.conn, out_path)
    assert n == 2

    expected_ts = int(datetime(2026, 5, 16, tzinfo=UTC).timestamp())
    out = sqlite3.connect(out_path)
    rows = out.execute(
        "SELECT game, segment, outcome, time_ms, created_at "
        "FROM attempts ORDER BY created_at, id"
    ).fetchall()
    assert rows == [
        ("smw", "YI1", "died", 1500, expected_ts),
        ("smw", "YI1", "survived", 6000, expected_ts),
    ]


def test_export_skips_invalidated_and_eventless_attempts(tmp_path):
    db = _build_db(tmp_path)
    # Invalidated.
    a1 = db.log_attempt(Attempt(
        segment_id="seg-1-1", session_id="sess", completed=True, time_ms=5000,
        invalidated=True, source="practice",
        created_at=datetime(2026, 5, 16, tzinfo=UTC),
    ))
    db.log_attempt_events(a1, [AttemptEvent(kind="survived", t_ms=5000)])
    # Aborted (no events).
    db.log_attempt(Attempt(
        segment_id="seg-1-1", session_id="sess", completed=False,
        source="practice", created_at=datetime(2026, 5, 16, tzinfo=UTC),
    ))
    # Non-practice source (reference seed).
    db.conn.execute(
        "INSERT INTO capture_runs (id, game_id, started_at) VALUES ('cr', 'smw', '2026-05-16')"
    )
    a3 = db.log_attempt(Attempt(
        segment_id="seg-1-1", capture_run_id="cr", completed=True, time_ms=4000,
        source="reference", created_at=datetime(2026, 5, 16, tzinfo=UTC),
    ))
    db.log_attempt_events(a3, [AttemptEvent(kind="survived", t_ms=4000)])

    out_path = tmp_path / "fixture.sqlite"
    n = export_to_fixture_sqlite(db.conn, out_path)
    assert n == 0
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/unit/test_export_fixture.py -v`
Expected: FAIL — `spinlab.tools.export_fixture` does not exist.

- [ ] **Step 3: Implement the exporter**

```python
# python/spinlab/tools/export_fixture.py
"""Export SpinLab practice attempts to the PGM-prototype fixture schema.

Cold-starts-only: writes one row per attempt, taking the event at
event_index = 0 (the first death OR the survival if the attempt was
deathless). Skips invalidated attempts, non-practice sources, and
attempts with zero recorded events (aborts).

Output schema is the canonical fixture schema defined in this plan's
"Fixture Schema" section — kept in sync with that block.
"""
from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime
from pathlib import Path

FIXTURE_SCHEMA = """
CREATE TABLE IF NOT EXISTS attempts (
    id         INTEGER PRIMARY KEY,
    game       TEXT NOT NULL,
    segment    TEXT NOT NULL,
    outcome    TEXT NOT NULL CHECK (outcome IN ('survived', 'died')),
    time_ms    INTEGER NOT NULL,
    created_at INTEGER NOT NULL DEFAULT (unixepoch())
);

CREATE INDEX IF NOT EXISTS ix_attempts_seg
    ON attempts (game, segment, created_at, id);
"""

EXPORT_QUERY = """
SELECT g.name AS game,
       s.description AS segment,
       e.kind AS outcome,
       e.t_ms AS time_ms,
       a.created_at AS created_at,
       a.id AS attempt_id
  FROM attempts a
  JOIN attempt_events e ON e.attempt_id = a.id AND e.event_index = 0
  JOIN segments s ON s.id = a.segment_id
  JOIN games g    ON g.id = s.game_id
 WHERE a.source = 'practice'
   AND a.invalidated = 0
 ORDER BY a.created_at, a.id
"""


def _iso_to_unix(iso: str) -> int:
    """SpinLab stores created_at as ISO TEXT; fixture wants unix int seconds."""
    return int(datetime.fromisoformat(iso).timestamp())


def export_to_fixture_sqlite(
    src_conn: sqlite3.Connection, dest_path: Path,
) -> int:
    """Write fixture sqlite at dest_path. Returns count of exported rows."""
    if dest_path.exists():
        dest_path.unlink()
    dest = sqlite3.connect(dest_path)
    try:
        dest.executescript(FIXTURE_SCHEMA)
        rows = src_conn.execute(EXPORT_QUERY).fetchall()
        dest.executemany(
            "INSERT INTO attempts (game, segment, outcome, time_ms, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [(r[0], r[1], r[2], r[3], _iso_to_unix(r[4])) for r in rows],
        )
        dest.commit()
        return len(rows)
    finally:
        dest.close()


def main() -> None:
    p = argparse.ArgumentParser(description="Export practice attempts to PGM fixture sqlite.")
    p.add_argument("--db", required=True, help="SpinLab database path")
    p.add_argument("--out", required=True, help="Output fixture sqlite path")
    args = p.parse_args()

    src = sqlite3.connect(args.db)
    try:
        n = export_to_fixture_sqlite(src, Path(args.out))
    finally:
        src.close()
    print(f"exported {n} attempts -> {args.out}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_export_fixture.py -v`
Expected: 2 passed.

Run the full fast suite to catch regressions:

Run: `python -m pytest -m "not emulator" -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```powershell
git add python/spinlab/tools/__init__.py python/spinlab/tools/export_fixture.py tests/unit/test_export_fixture.py
git commit -m "feat(tools): export_fixture command for PGM cold-start fixture"
```

---

## Task 7: Final verification — full suite + integration sanity

- [ ] **Step 1: Run the full test suite**

Per [CLAUDE.md](../../../CLAUDE.md): `python -m pytest` (NOT `pytest -m "not emulator"`).

Expected: all PASS. Investigate any failures — likely candidates are integration tests that consume `AttemptResultEvent` and now see a non-empty `events` tuple.

- [ ] **Step 2: Hand-run the exporter on an empty DB**

```powershell
spinlab db reset
python -m spinlab.tools.export_fixture --db (Resolve-Path "$env:USERPROFILE/AppData/Local/spinlab/spinlab.sqlite") --out C:/tmp/fixture.sqlite
```

Expected: `exported 0 attempts -> C:/tmp/fixture.sqlite`. Open `C:/tmp/fixture.sqlite` to verify the `attempts` table exists with the right shape.

- [ ] **Step 3: Commit any cleanup**

```powershell
git status
# If anything additional needed cleanup, commit it here.
```

---

## Task 8: Operator runbook (no code — capture session protocol)

This task documents the workflow for capturing fixture data. Not executable; reference for Andrew when the time comes.

**Sequence (per Andrew, 2026-05-16):**

1. **Debug-game session first.** Pick a familiar game. Reset DB (`spinlab db reset`). Play through 1-2 segments with deliberate deaths. After the session, query: `SELECT a.id, a.deaths, COUNT(e.id) FROM attempts a LEFT JOIN attempt_events e ON e.attempt_id = a.id GROUP BY a.id`. Verify that `deaths + (completed=1 ? 1 : 0) == event_count` for each attempt. If not, the event capture is wrong — fix before continuing.

2. **New-game cold run (priority — false-start-bug hunting).** Reset DB. Pick a new kaizo hack with small-only counter-break. Play for 1-3 hours with frequent deaths. Inspect the resulting `attempts` + `attempt_events` for plausibility; surface any new bugs.

3. **Known-game practice run.** Reset DB. Pick a familiar 6-level segment set. Practice for 2-5 hours, naturally varying attempt counts per segment. Confirm `attempts.source = 'practice'` for everything (no accidental reference/replay rows).

4. **Export the fixture.**

   ```powershell
   python -m spinlab.tools.export_fixture --db <spinlab db> --out C:/tmp/spinlab-fixture.sqlite
   ```

5. **Hand to the PGM-prototype project.** The fixture sqlite matches the canonical "Fixture Schema" section at the top of this plan.

**Notes:**
- DB reset between (1), (2), and (3) keeps the fixture clean. Per [feedback_run_emulator_tests.md](../../../C:/Users/thedo/.claude/projects/c--Users-thedo-git-spinlab/memory/feedback_run_emulator_tests.md), make sure the emulator harness is healthy first.
- 3-8 hours total across 2-4 games is the target fixture size per Andrew.

---

## Self-Review Checklist

- [x] Spec coverage: every "Open design question" maps to either a deliberate plan choice or a deferred decision.
- [x] No placeholders: every code step shows actual code; every test shows actual assertions.
- [x] Type consistency: `AttemptEvent(kind, t_ms)` used identically across Tasks 2, 3, 4, 5, 6.
- [x] File paths: all paths absolute or repo-relative.
- [x] TDD: every implementation task has a red-test step before the green-code step.
- [x] Commits: frequent (one per task), all use HEREDOC-free `git commit -m` for PowerShell compatibility.

## Execution Handoff

**NOT YET — this plan is deferred.** Re-read the STATUS block at the top of this file before executing. When ready, two execution options:

1. **Subagent-Driven (recommended)** — fresh subagent per task, review between tasks.
2. **Inline Execution** — execute in one session with checkpoints.

Decide at execution time based on how much the surrounding code has drifted since this was written.
