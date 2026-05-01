# Multi-Session Reference Runs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enable reference runs to span multiple recording sessions across hours/days, surviving dashboard restarts and Mesen crashes, while preserving single-session ergonomics.

**Architecture:** Introduce a `capture_sessions` table (1:N child of `capture_runs`) and a `recorded_segment_times` buffer table that persists per-segment timing immediately. Stop becomes non-destructive (parks the run as paused), Resume creates a new session under the existing run, Finalize drains buffered timings into `attempts`. Single-session users get a "Save & Finish Run" combined action that preserves today's one-shot flow. Several latent refactors come along: the `_end_current_session` helper consolidates three duplicate code paths, in-memory recorder state is replaced with DB-derived counts, and `DraftManager` is dissolved.

**Tech Stack:** Python 3.11 + SQLite (sqlite3 module), FastAPI, asyncio, TypeScript + Vite, Lua (Mesen `--testRunner`), pytest + Playwright (async).

**Spec:** [docs/superpowers/specs/2026-05-01-multi-session-reference-runs-design.md](../specs/2026-05-01-multi-session-reference-runs-design.md)

**Pre-flight:**
- This plan should be executed in a dedicated git worktree (see `superpowers:using-git-worktrees`). It touches DB schema, capture controllers, FastAPI routes, frontend, and Lua — large blast radius.
- Before starting, run the **full** test suite from main and capture the baseline: `python -m pytest`. Note any pre-existing failures so you can distinguish them from regressions you introduce. Per `feedback_fix_preexisting_failures.md`, fix all pre-existing failures both before AND after.
- Greenfield DB: no migration. The existing `db/core.py:_init_schema` will detect column-set drift and rebuild affected tables. New tables are added to `SCHEMA`; updated tables get their `expected_columns` updated.

---

## File Structure

### New files

| Path | Responsibility |
|---|---|
| `python/spinlab/db/capture_sessions.py` | `CaptureSessionsMixin`: CRUD + recovery for `capture_sessions` rows |
| `python/spinlab/db/recorded_segment_times.py` | `RecordedSegmentTimesMixin`: append + drain buffered timing data |
| `tests/unit/db/test_db_capture_sessions.py` | Unit tests for the new sessions mixin |
| `tests/unit/db/test_db_recorded_segment_times.py` | Unit tests for the buffer mixin |
| `tests/unit/capture/test_multi_session.py` | Multi-session lifecycle, save_and_finish, delete_session |
| `tests/integration/test_crash_recovery.py` | Python-only crash + recover integration test |
| `tests/integration/test_multi_session_smoke.py` | Playwright smoke for resume after dashboard restart |

### Modified files

| Path | Change |
|---|---|
| `python/spinlab/db/core.py` | Add `capture_sessions`, `recorded_segment_times` to SCHEMA. Add `capture_session_id` to `segments`. Update `_expected_columns` for `segments`. New indexes. |
| `python/spinlab/db/__init__.py` | Wire two new mixins into `Database` |
| `python/spinlab/db/capture_runs.py` | Extend `hard_delete_capture_run` to remove sessions, recorded_segment_times, and spinrec files from disk |
| `python/spinlab/db/segments.py` | `upsert_segment` carries `capture_session_id`; `get_segments_by_reference` selects it |
| `python/spinlab/models.py` | New `CaptureSession` dataclass; `Segment` adds `capture_session_id: str | None = None` |
| `python/spinlab/capture/recorder.py` | Drop in-memory `segments_count`, `segment_times`, `RecordedSegmentTime`, `enter_draft()`. Take `current_capture_session_id`. Write timing to DB on segment close. |
| `python/spinlab/capture/reference.py` | New state model (RECORDING/PAUSED), new methods (`resume`, `finalize`, `save_and_finish`, `discard_run`, `delete_capture_session`), `_end_current_session` helper, `recover_paused_run` |
| `python/spinlab/capture/draft.py` | Dissolve. Move `_seed_reference_attempts` (drain version) into `reference.py`. Delete file. |
| `python/spinlab/capture/__init__.py` | Drop `DraftManager` and `RecordedSegmentTime` exports |
| `python/spinlab/session_manager.py` | Update proxies. Replace `save_draft`/`discard_draft` with `finalize_run`/`save_and_finish`/`discard_run`/`resume_reference`/`delete_capture_session`. Update `_clear_ref_and_idle` references. Update `recover` call. |
| `python/spinlab/routes/reference.py` | Rename `draft/save` → `reference/finalize`, `draft/discard` → `reference/discard_run`. Add `reference/resume`, `reference/save_and_finish`, `DELETE /capture_sessions/{id}`. Add list-sessions endpoint. |
| `python/spinlab/state_builder.py` | Replace `draft` field in state with `paused_run` (run id, name, session count, segments captured) |
| `python/spinlab/errors.py` | Add `SessionDeleteAfterFinalizeError`; rename `DraftPendingError` to `RunPendingError` (or add alias) |
| `lua/spinlab.lua` | Remove `MAX_RECORDING_FRAMES` constant (line 25) and the cap-hit branch in `on_input_polled` (lines 1276-1287) |
| `frontend/src/types.ts` | New `CaptureSession` type. `AppState.draft` → `AppState.paused_run` with new shape. Add `sessions` to `Reference`. |
| `frontend/src/manage.ts` | Rework reference panel: paused-run card with session sublist, new buttons. |
| `frontend/index.html` | New DOM for paused-run panel and session list |
| `frontend/style.css` | Styles for new elements |
| `tests/unit/capture/test_reference.py` | Update disconnect/stop assertions for new behavior |
| `tests/unit/capture/test_draft.py` | Delete file (logic merged into test_multi_session.py) |
| `tests/unit/test_session_manager.py` | Update for renamed methods |
| `tests/factories.py` | Add `make_capture_session(...)` helper |

---

## Phase 1: DB layer

### Task 1: Add `capture_sessions` and `recorded_segment_times` tables to SCHEMA

**Files:**
- Modify: `python/spinlab/db/core.py`

- [ ] **Step 1: Add the new table definitions to `SCHEMA`**

In `python/spinlab/db/core.py`, after the `capture_runs` table definition (around line 114), add:

```sql
CREATE TABLE IF NOT EXISTS capture_sessions (
  id TEXT PRIMARY KEY,
  capture_run_id TEXT NOT NULL REFERENCES capture_runs(id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  spinrec_path TEXT NOT NULL,
  end_reason TEXT,
  UNIQUE (capture_run_id, ordinal)
);

CREATE TABLE IF NOT EXISTS recorded_segment_times (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  capture_session_id TEXT NOT NULL REFERENCES capture_sessions(id) ON DELETE CASCADE,
  segment_id TEXT NOT NULL,
  time_ms INTEGER NOT NULL,
  deaths INTEGER NOT NULL,
  clean_tail_ms INTEGER NOT NULL,
  recorded_at TEXT NOT NULL
);
```

- [ ] **Step 2: Add `capture_session_id` to the `segments` table definition in SCHEMA**

In the `segments` CREATE TABLE block, add a column after `reference_id`:

```sql
  capture_session_id TEXT REFERENCES capture_sessions(id) ON DELETE CASCADE,
```

- [ ] **Step 3: Update `_expected_columns` for `segments`**

In `_expected_columns`, update the `segments` set to include `"capture_session_id"`:

```python
"segments": {"id", "game_id", "level_number", "start_type", "start_ordinal",
             "end_type", "end_ordinal", "start_waypoint_id", "end_waypoint_id",
             "is_primary", "description", "strat_version", "active", "ordinal",
             "reference_id", "capture_session_id", "created_at", "updated_at"},
```

This causes the existing rebuild-on-mismatch logic to drop and recreate `segments` on next startup, picking up the new column.

- [ ] **Step 4: Add indexes**

After the existing `CREATE INDEX` lines in SCHEMA, add:

```sql
CREATE INDEX IF NOT EXISTS idx_capture_sessions_run ON capture_sessions(capture_run_id, ordinal);
CREATE INDEX IF NOT EXISTS idx_recorded_segment_times_session ON recorded_segment_times(capture_session_id);
CREATE INDEX IF NOT EXISTS idx_segments_capture_session ON segments(capture_session_id);
```

- [ ] **Step 5: Verify schema applies**

Run: `python -c "from spinlab.db import Database; d = Database(':memory:'); print([r[0] for r in d.conn.execute(\"SELECT name FROM sqlite_master WHERE type='table' ORDER BY name\")])"`

Expected output (alphabetical): includes `capture_sessions` and `recorded_segment_times`. Verify `segments` has `capture_session_id`:

`python -c "from spinlab.db import Database; d = Database(':memory:'); print([r[1] for r in d.conn.execute('PRAGMA table_info(segments)')])"`

Expected: list includes `capture_session_id`.

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/db/core.py
git commit -m "feat(db): add capture_sessions, recorded_segment_times tables"
```

---

### Task 2: `CaptureSessionsMixin` — basic CRUD

**Files:**
- Create: `python/spinlab/db/capture_sessions.py`
- Modify: `python/spinlab/db/__init__.py`
- Test: `tests/unit/db/test_db_capture_sessions.py`

- [ ] **Step 1: Write the failing test for `create_capture_session` and `get_capture_session`**

Create `tests/unit/db/test_db_capture_sessions.py`:

```python
"""Tests for CaptureSessionsMixin."""
import pytest
from spinlab.db import Database


@pytest.fixture
def db():
    d = Database(":memory:")
    d.upsert_game("smw", "Super Mario World", "any%")
    d.create_capture_run("run_1", "smw", "Test Run", draft=True)
    yield d
    d.close()


def test_create_and_get_capture_session(db):
    db.create_capture_session(
        session_id="sess_1", capture_run_id="run_1",
        ordinal=1, spinrec_path="/tmp/sess_1.spinrec",
    )
    sess = db.get_capture_session("sess_1")
    assert sess is not None
    assert sess["id"] == "sess_1"
    assert sess["capture_run_id"] == "run_1"
    assert sess["ordinal"] == 1
    assert sess["spinrec_path"] == "/tmp/sess_1.spinrec"
    assert sess["ended_at"] is None
    assert sess["end_reason"] is None


def test_get_capture_session_missing_returns_none(db):
    assert db.get_capture_session("nonexistent") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/db/test_db_capture_sessions.py -v`
Expected: FAIL with `AttributeError: 'Database' object has no attribute 'create_capture_session'`.

- [ ] **Step 3: Create the mixin with create + get**

Create `python/spinlab/db/capture_sessions.py`:

```python
"""Capture session (recording session within a capture run) queries.

Note on naming: `capture_sessions` are recording sessions within a multi-session
reference run. They are distinct from `sessions` (practice sessions) and from
`attempts.session_id` (polymorphic parent grouping). All three exist; read carefully.
"""
import sqlite3
from datetime import UTC, datetime
from typing import TypedDict


class CaptureSessionRow(TypedDict):
    id: str
    capture_run_id: str
    ordinal: int
    started_at: str
    ended_at: str | None
    spinrec_path: str
    end_reason: str | None


class CaptureSessionsMixin:
    """CRUD and recovery for capture_sessions."""
    conn: sqlite3.Connection

    def create_capture_session(
        self, session_id: str, capture_run_id: str,
        ordinal: int, spinrec_path: str,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "INSERT INTO capture_sessions "
            "(id, capture_run_id, ordinal, started_at, spinrec_path) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, capture_run_id, ordinal, now, spinrec_path),
        )
        self.conn.commit()

    def get_capture_session(self, session_id: str) -> CaptureSessionRow | None:
        row = self.conn.execute(
            "SELECT id, capture_run_id, ordinal, started_at, ended_at, "
            "spinrec_path, end_reason FROM capture_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        return dict(row)  # type: ignore[return-value]
```

- [ ] **Step 4: Wire mixin into `Database`**

In `python/spinlab/db/__init__.py`, add:

```python
from .capture_sessions import CaptureSessionsMixin
```

and add `CaptureSessionsMixin` to the `Database` class bases (after `CaptureRunsMixin`):

```python
class Database(
    WaypointsMixin,
    SegmentsMixin,
    AttemptsMixin,
    SessionsMixin,
    ModelStateMixin,
    CaptureRunsMixin,
    CaptureSessionsMixin,
    DatabaseCore,
):
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/unit/db/test_db_capture_sessions.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/db/capture_sessions.py python/spinlab/db/__init__.py tests/unit/db/test_db_capture_sessions.py
git commit -m "feat(db): CaptureSessionsMixin with create + get"
```

---

### Task 3: `CaptureSessionsMixin` — end_session, list_for_run, mark_orphans_crashed

**Files:**
- Modify: `python/spinlab/db/capture_sessions.py`
- Modify: `tests/unit/db/test_db_capture_sessions.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/unit/db/test_db_capture_sessions.py`:

```python
def test_end_capture_session_sets_ended_at_and_reason(db):
    db.create_capture_session("sess_1", "run_1", 1, "/tmp/x.spinrec")
    db.end_capture_session("sess_1", end_reason="stopped")
    sess = db.get_capture_session("sess_1")
    assert sess["ended_at"] is not None
    assert sess["end_reason"] == "stopped"


def test_list_capture_sessions_for_run_orders_by_ordinal(db):
    db.create_capture_session("sess_a", "run_1", 2, "/tmp/a.spinrec")
    db.create_capture_session("sess_b", "run_1", 1, "/tmp/b.spinrec")
    db.create_capture_session("sess_c", "run_1", 3, "/tmp/c.spinrec")
    sessions = db.list_capture_sessions_for_run("run_1")
    assert [s["id"] for s in sessions] == ["sess_b", "sess_a", "sess_c"]
    assert [s["ordinal"] for s in sessions] == [1, 2, 3]


def test_mark_orphan_capture_sessions_crashed(db):
    # Two open sessions and one already-ended
    db.create_capture_session("sess_a", "run_1", 1, "/tmp/a.spinrec")
    db.end_capture_session("sess_a", end_reason="stopped")
    db.create_capture_session("sess_b", "run_1", 2, "/tmp/b.spinrec")
    db.create_capture_session("sess_c", "run_1", 3, "/tmp/c.spinrec")
    count = db.mark_orphan_capture_sessions_crashed("run_1")
    assert count == 2
    assert db.get_capture_session("sess_a")["end_reason"] == "stopped"
    assert db.get_capture_session("sess_b")["end_reason"] == "crashed"
    assert db.get_capture_session("sess_b")["ended_at"] is not None
    assert db.get_capture_session("sess_c")["end_reason"] == "crashed"


def test_max_session_ordinal_for_run(db):
    assert db.max_session_ordinal_for_run("run_1") == 0
    db.create_capture_session("sess_1", "run_1", 1, "/tmp/1.spinrec")
    db.create_capture_session("sess_2", "run_1", 2, "/tmp/2.spinrec")
    assert db.max_session_ordinal_for_run("run_1") == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/db/test_db_capture_sessions.py -v`
Expected: 4 new failures with `AttributeError`.

- [ ] **Step 3: Implement the new methods**

Append to `python/spinlab/db/capture_sessions.py`:

```python
    def end_capture_session(self, session_id: str, end_reason: str) -> None:
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "UPDATE capture_sessions SET ended_at = ?, end_reason = ? "
            "WHERE id = ? AND ended_at IS NULL",
            (now, end_reason, session_id),
        )
        self.conn.commit()

    def list_capture_sessions_for_run(self, capture_run_id: str) -> list[CaptureSessionRow]:
        rows = self.conn.execute(
            "SELECT id, capture_run_id, ordinal, started_at, ended_at, "
            "spinrec_path, end_reason FROM capture_sessions "
            "WHERE capture_run_id = ? ORDER BY ordinal",
            (capture_run_id,),
        ).fetchall()
        return [dict(r) for r in rows]  # type: ignore[return-value]

    def mark_orphan_capture_sessions_crashed(self, capture_run_id: str) -> int:
        """Mark any open sessions (ended_at IS NULL) as crashed. Returns count updated."""
        now = datetime.now(UTC).isoformat()
        cur = self.conn.execute(
            "UPDATE capture_sessions SET ended_at = ?, end_reason = 'crashed' "
            "WHERE capture_run_id = ? AND ended_at IS NULL",
            (now, capture_run_id),
        )
        self.conn.commit()
        return cur.rowcount

    def max_session_ordinal_for_run(self, capture_run_id: str) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(ordinal), 0) FROM capture_sessions "
            "WHERE capture_run_id = ?",
            (capture_run_id,),
        ).fetchone()
        return int(row[0])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/db/test_db_capture_sessions.py -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/db/capture_sessions.py tests/unit/db/test_db_capture_sessions.py
git commit -m "feat(db): end_session, list_for_run, orphan recovery, max_ordinal"
```

---

### Task 4: `CaptureSessionsMixin` — delete_capture_session

**Files:**
- Modify: `python/spinlab/db/capture_sessions.py`
- Modify: `tests/unit/db/test_db_capture_sessions.py`

- [ ] **Step 1: Write failing test**

Append to `tests/unit/db/test_db_capture_sessions.py`:

```python
def test_delete_capture_session_removes_row(db):
    db.create_capture_session("sess_1", "run_1", 1, "/tmp/x.spinrec")
    db.delete_capture_session("sess_1")
    assert db.get_capture_session("sess_1") is None


def test_delete_capture_session_cascades_to_recorded_segment_times(db):
    db.create_capture_session("sess_1", "run_1", 1, "/tmp/x.spinrec")
    db.add_recorded_segment_time("sess_1", "seg_x", time_ms=1000, deaths=0, clean_tail_ms=1000)
    rows = db.conn.execute(
        "SELECT COUNT(*) FROM recorded_segment_times WHERE capture_session_id = ?",
        ("sess_1",),
    ).fetchone()
    assert rows[0] == 1
    db.delete_capture_session("sess_1")
    rows = db.conn.execute(
        "SELECT COUNT(*) FROM recorded_segment_times WHERE capture_session_id = ?",
        ("sess_1",),
    ).fetchone()
    assert rows[0] == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/db/test_db_capture_sessions.py::test_delete_capture_session_removes_row -v`
Expected: FAIL with `AttributeError: 'Database' object has no attribute 'delete_capture_session'`.

(The cascade test will also fail because `add_recorded_segment_time` doesn't exist yet — that's added in Task 6. We expect both to fail at this point; we'll come back and verify after Task 6.)

- [ ] **Step 3: Implement delete_capture_session**

Append to `python/spinlab/db/capture_sessions.py`:

```python
    def delete_capture_session(self, session_id: str) -> None:
        """Delete a capture session. FK ON DELETE CASCADE removes related rows."""
        self.conn.execute(
            "DELETE FROM capture_sessions WHERE id = ?", (session_id,),
        )
        self.conn.commit()
```

- [ ] **Step 4: Run the simpler test only**

Run: `pytest tests/unit/db/test_db_capture_sessions.py::test_delete_capture_session_removes_row -v`
Expected: PASS.

The cascade test is parked — re-run after Task 6.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/db/capture_sessions.py tests/unit/db/test_db_capture_sessions.py
git commit -m "feat(db): delete_capture_session"
```

---

### Task 5: `RecordedSegmentTimesMixin` — add + drain + cascade

**Files:**
- Create: `python/spinlab/db/recorded_segment_times.py`
- Modify: `python/spinlab/db/__init__.py`
- Test: `tests/unit/db/test_db_recorded_segment_times.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/db/test_db_recorded_segment_times.py`:

```python
"""Tests for RecordedSegmentTimesMixin."""
import pytest
from spinlab.db import Database


@pytest.fixture
def db():
    d = Database(":memory:")
    d.upsert_game("smw", "Super Mario World", "any%")
    d.create_capture_run("run_1", "smw", "Test Run", draft=True)
    d.create_capture_session("sess_1", "run_1", 1, "/tmp/1.spinrec")
    d.create_capture_session("sess_2", "run_1", 2, "/tmp/2.spinrec")
    yield d
    d.close()


def test_add_and_drain_recorded_segment_times(db):
    db.add_recorded_segment_time("sess_1", "seg_a", time_ms=1000, deaths=0, clean_tail_ms=1000)
    db.add_recorded_segment_time("sess_2", "seg_b", time_ms=2000, deaths=1, clean_tail_ms=500)
    drained = db.drain_recorded_segment_times_for_run("run_1")
    assert len(drained) == 2
    by_seg = {r["segment_id"]: r for r in drained}
    assert by_seg["seg_a"]["time_ms"] == 1000
    assert by_seg["seg_a"]["deaths"] == 0
    assert by_seg["seg_a"]["clean_tail_ms"] == 1000
    assert by_seg["seg_b"]["time_ms"] == 2000
    assert by_seg["seg_b"]["deaths"] == 1
    assert by_seg["seg_b"]["clean_tail_ms"] == 500
    # Drain deletes
    rows = db.conn.execute("SELECT COUNT(*) FROM recorded_segment_times").fetchone()
    assert rows[0] == 0


def test_drain_only_pulls_from_specified_run(db):
    db.create_capture_run("run_other", "smw", "Other", draft=True)
    db.create_capture_session("sess_other", "run_other", 1, "/tmp/o.spinrec")
    db.add_recorded_segment_time("sess_1", "seg_x", time_ms=100, deaths=0, clean_tail_ms=100)
    db.add_recorded_segment_time("sess_other", "seg_y", time_ms=200, deaths=0, clean_tail_ms=200)
    drained = db.drain_recorded_segment_times_for_run("run_1")
    assert len(drained) == 1
    assert drained[0]["segment_id"] == "seg_x"
    # The other run's row remains
    rows = db.conn.execute("SELECT COUNT(*) FROM recorded_segment_times").fetchone()
    assert rows[0] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/db/test_db_recorded_segment_times.py -v`
Expected: FAIL with `AttributeError: 'Database' object has no attribute 'add_recorded_segment_time'`.

- [ ] **Step 3: Create the mixin**

Create `python/spinlab/db/recorded_segment_times.py`:

```python
"""Recorded segment times — per-segment timing buffer.

Populated immediately as segments close during a recording session, drained
into `attempts` at finalize time. Enables crash-safe timing data without
polluting the `attempts` table with provisional rows.
"""
import sqlite3
from datetime import UTC, datetime
from typing import TypedDict


class RecordedSegmentTimeRow(TypedDict):
    id: int
    capture_session_id: str
    segment_id: str
    time_ms: int
    deaths: int
    clean_tail_ms: int
    recorded_at: str


class RecordedSegmentTimesMixin:
    """Per-segment timing buffer for in-progress reference runs."""
    conn: sqlite3.Connection

    def add_recorded_segment_time(
        self, capture_session_id: str, segment_id: str,
        time_ms: int, deaths: int, clean_tail_ms: int,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "INSERT INTO recorded_segment_times "
            "(capture_session_id, segment_id, time_ms, deaths, clean_tail_ms, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (capture_session_id, segment_id, time_ms, deaths, clean_tail_ms, now),
        )
        self.conn.commit()

    def drain_recorded_segment_times_for_run(self, capture_run_id: str) -> list[RecordedSegmentTimeRow]:
        """Return all timing rows for the run, then delete them. Atomic."""
        rows = self.conn.execute(
            "SELECT t.id, t.capture_session_id, t.segment_id, t.time_ms, "
            "t.deaths, t.clean_tail_ms, t.recorded_at "
            "FROM recorded_segment_times t "
            "JOIN capture_sessions s ON t.capture_session_id = s.id "
            "WHERE s.capture_run_id = ? ORDER BY t.id",
            (capture_run_id,),
        ).fetchall()
        result = [dict(r) for r in rows]
        ids = [r["id"] for r in result]
        if ids:
            placeholders = ",".join("?" * len(ids))
            self.conn.execute(
                f"DELETE FROM recorded_segment_times WHERE id IN ({placeholders})", ids,
            )
        self.conn.commit()
        return result  # type: ignore[return-value]
```

- [ ] **Step 4: Wire into `Database`**

In `python/spinlab/db/__init__.py`, add:

```python
from .recorded_segment_times import RecordedSegmentTimesMixin
```

and add `RecordedSegmentTimesMixin` to the `Database` class bases:

```python
class Database(
    WaypointsMixin,
    SegmentsMixin,
    AttemptsMixin,
    SessionsMixin,
    ModelStateMixin,
    CaptureRunsMixin,
    CaptureSessionsMixin,
    RecordedSegmentTimesMixin,
    DatabaseCore,
):
```

- [ ] **Step 5: Run all DB tests to verify**

Run: `pytest tests/unit/db/test_db_recorded_segment_times.py tests/unit/db/test_db_capture_sessions.py -v`
Expected: All PASS, including the previously-parked `test_delete_capture_session_cascades_to_recorded_segment_times`.

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/db/recorded_segment_times.py python/spinlab/db/__init__.py tests/unit/db/test_db_recorded_segment_times.py
git commit -m "feat(db): RecordedSegmentTimesMixin with add + drain"
```

---

### Task 6: Extend `hard_delete_capture_run` to remove sessions, timings, and spinrec files

**Files:**
- Modify: `python/spinlab/db/capture_runs.py`
- Modify: `tests/unit/db/test_db_capture_sessions.py`

- [ ] **Step 1: Write failing test for hard_delete cascading through sessions**

Append to `tests/unit/db/test_db_capture_sessions.py`:

```python
def test_hard_delete_capture_run_cascades_to_sessions_and_times(db):
    db.create_capture_session("sess_1", "run_1", 1, "/tmp/1.spinrec")
    db.create_capture_session("sess_2", "run_1", 2, "/tmp/2.spinrec")
    db.add_recorded_segment_time("sess_1", "seg_a", time_ms=100, deaths=0, clean_tail_ms=100)
    db.hard_delete_capture_run("run_1")
    assert db.list_capture_sessions_for_run("run_1") == []
    rows = db.conn.execute("SELECT COUNT(*) FROM recorded_segment_times").fetchone()
    assert rows[0] == 0


def test_hard_delete_capture_run_removes_spinrec_files(tmp_path, db):
    spinrec_a = tmp_path / "a.spinrec"
    spinrec_b = tmp_path / "b.spinrec"
    spinrec_a.write_bytes(b"x")
    spinrec_b.write_bytes(b"y")
    db.create_capture_session("sess_1", "run_1", 1, str(spinrec_a))
    db.create_capture_session("sess_2", "run_1", 2, str(spinrec_b))
    db.hard_delete_capture_run("run_1")
    assert not spinrec_a.exists()
    assert not spinrec_b.exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/db/test_db_capture_sessions.py::test_hard_delete_capture_run_cascades_to_sessions_and_times tests/unit/db/test_db_capture_sessions.py::test_hard_delete_capture_run_removes_spinrec_files -v`
Expected: 1 PASS (cascade — FK ON DELETE CASCADE handles it for free), 1 FAIL (spinrec files not removed).

If both pass, the FK cascade is doing the work — note that and proceed to add file removal.

- [ ] **Step 3: Extend `hard_delete_capture_run` to remove spinrec files**

In `python/spinlab/db/capture_runs.py`, modify `hard_delete_capture_run`:

```python
    def hard_delete_capture_run(self, run_id: str) -> None:
        """Hard delete: remove run, segments, model_state, attempts, sessions, recorded times, and spinrec files."""
        from pathlib import Path

        # Collect spinrec paths before deleting
        session_paths = [
            r[0] for r in self.conn.execute(
                "SELECT spinrec_path FROM capture_sessions WHERE capture_run_id = ?",
                (run_id,),
            ).fetchall()
        ]

        seg_ids = [
            r[0] for r in self.conn.execute(
                "SELECT id FROM segments WHERE reference_id = ?", (run_id,)
            ).fetchall()
        ]
        if seg_ids:
            placeholders = ",".join("?" * len(seg_ids))
            self.conn.execute(
                f"DELETE FROM model_state WHERE segment_id IN ({placeholders})",
                seg_ids,
            )
            self.conn.execute(
                f"DELETE FROM attempts WHERE segment_id IN ({placeholders})",
                seg_ids,
            )
            self.conn.execute(
                "DELETE FROM segments WHERE reference_id = ?", (run_id,),
            )
        # capture_sessions and recorded_segment_times cascade via FK ON DELETE CASCADE
        self.conn.execute("DELETE FROM capture_runs WHERE id = ?", (run_id,))
        self.conn.commit()

        # Remove spinrec files from disk
        for path_str in session_paths:
            try:
                Path(path_str).unlink(missing_ok=True)
            except OSError:
                pass  # File may be locked or already gone — best-effort cleanup
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/db/test_db_capture_sessions.py -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/db/capture_runs.py tests/unit/db/test_db_capture_sessions.py
git commit -m "feat(db): hard_delete_capture_run cascades to sessions and spinrec files"
```

---

### Task 7: `Segment` model gains `capture_session_id`; `upsert_segment` and `get_segments_by_reference` update

**Files:**
- Modify: `python/spinlab/models.py`
- Modify: `python/spinlab/db/segments.py`
- Modify: `python/spinlab/db/capture_runs.py` (the `get_segments_by_reference` method lives here)
- Modify: `python/spinlab/db/core.py` (the `_row_to_segment` helper, if it lives there)
- Test: `tests/unit/db/test_db_segments.py` (or sibling, find existing)

- [ ] **Step 1: Find where `_row_to_segment` is defined**

Run: `rg "_row_to_segment" python/spinlab/db/ -n`

Note the location; you'll need to update it to read `capture_session_id`.

- [ ] **Step 2: Write failing test for segment carrying capture_session_id**

Locate the existing `tests/unit/db/test_db_segments.py` (or similar). Append:

```python
def test_segment_persists_capture_session_id():
    from spinlab.db import Database
    from spinlab.models import EndpointType, Segment, Waypoint
    d = Database(":memory:")
    d.upsert_game("smw", "Super Mario World", "any%")
    d.create_capture_run("run_1", "smw", "Test Run", draft=True)
    d.create_capture_session("sess_1", "run_1", 1, "/tmp/1.spinrec")
    wp_a = Waypoint.make("smw", 1, EndpointType.ENTRANCE, 0, {})
    wp_b = Waypoint.make("smw", 1, EndpointType.GOAL, 0, {})
    d.upsert_waypoint(wp_a)
    d.upsert_waypoint(wp_b)
    seg = Segment(
        id="seg_x", game_id="smw", level_number=1,
        start_type=EndpointType.ENTRANCE, start_ordinal=0,
        end_type=EndpointType.GOAL, end_ordinal=0,
        start_waypoint_id=wp_a.id, end_waypoint_id=wp_b.id,
        reference_id="run_1", capture_session_id="sess_1",
    )
    d.upsert_segment(seg)
    fetched = d.get_segment_by_id("seg_x")
    assert fetched is not None
    assert fetched.capture_session_id == "sess_1"
    d.close()
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/unit/db/test_db_segments.py::test_segment_persists_capture_session_id -v`
Expected: FAIL — either `Segment.__init__` rejects `capture_session_id` or the column isn't read back.

- [ ] **Step 4: Add `capture_session_id` to the `Segment` dataclass**

In `python/spinlab/models.py`, add after `is_primary`:

```python
    capture_session_id: Optional[str] = None
```

- [ ] **Step 5: Update `upsert_segment` to write `capture_session_id`**

In `python/spinlab/db/segments.py`, update the INSERT/UPDATE SQL and parameter tuple. Add `capture_session_id` to the column list, the placeholders, the `ON CONFLICT DO UPDATE SET` clause, and the parameter tuple:

```python
    def upsert_segment(self, seg: Segment) -> None:
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            """INSERT INTO segments (id, game_id, level_number, start_type, start_ordinal,
               end_type, end_ordinal, start_waypoint_id, end_waypoint_id, is_primary,
               description, strat_version, active, ordinal,
               reference_id, capture_session_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 description=excluded.description,
                 ordinal=excluded.ordinal,
                 reference_id=excluded.reference_id,
                 capture_session_id=excluded.capture_session_id,
                 active=excluded.active,
                 is_primary=excluded.is_primary,
                 updated_at=excluded.updated_at""",
            (seg.id, seg.game_id, seg.level_number, seg.start_type,
             seg.start_ordinal, seg.end_type, seg.end_ordinal,
             seg.start_waypoint_id, seg.end_waypoint_id, int(seg.is_primary),
             seg.description, seg.strat_version, int(seg.active),
             seg.ordinal, seg.reference_id, seg.capture_session_id, now, now),
        )
        self.conn.commit()
```

- [ ] **Step 6: Update `_row_to_segment` to read `capture_session_id`**

In wherever `_row_to_segment` is defined (likely `python/spinlab/db/core.py:240ish`), add `capture_session_id` to the constructed Segment:

```python
            capture_session_id=row["capture_session_id"] if "capture_session_id" in keys else None,
```

(Match the surrounding pattern for nullable fields.)

- [ ] **Step 7: Update `get_segments_by_reference` to select `capture_session_id`**

In `python/spinlab/db/capture_runs.py`, modify `get_segments_by_reference`'s SELECT to include `capture_session_id`:

```python
        cur = self.conn.execute(
            """SELECT id, game_id, level_number, start_type, start_ordinal,
                      end_type, end_ordinal, description, active, ordinal,
                      reference_id, capture_session_id,
                      NULL AS state_path
               FROM segments WHERE reference_id = ? AND active = 1
               ORDER BY ordinal""",
            (reference_id,),
        )
```

And update the `ReferenceSegmentRow` TypedDict (top of `capture_runs.py`) to include `capture_session_id: str | None`.

- [ ] **Step 8: Run test to verify it passes**

Run: `pytest tests/unit/db/test_db_segments.py::test_segment_persists_capture_session_id -v`
Expected: PASS.

Also run the full DB test suite to catch regressions:

Run: `pytest tests/unit/db/ -v`
Expected: All PASS.

- [ ] **Step 9: Commit**

```bash
git add python/spinlab/models.py python/spinlab/db/segments.py python/spinlab/db/capture_runs.py python/spinlab/db/core.py tests/unit/db/test_db_segments.py
git commit -m "feat(db): segment carries capture_session_id"
```

---

### Task 8: Recovery method on the DB layer (`recover_paused_capture_run`)

**Files:**
- Modify: `python/spinlab/db/capture_sessions.py`
- Modify: `tests/unit/db/test_db_capture_sessions.py`

- [ ] **Step 1: Write failing test**

Append to `tests/unit/db/test_db_capture_sessions.py`:

```python
def test_recover_paused_capture_run_finds_most_recent_draft(db):
    # Two draft runs for same game; recover picks most recent and removes others
    import time
    db.create_capture_run("run_old", "smw", "Old", draft=True)
    time.sleep(0.01)  # ensure different created_at
    db.create_capture_run("run_new", "smw", "New", draft=True)
    found = db.recover_paused_capture_run("smw")
    assert found == "run_new"
    # Old draft is gone
    rows = db.conn.execute("SELECT id FROM capture_runs").fetchall()
    assert {r[0] for r in rows} == {"run_1", "run_new"}  # run_1 from fixture, run_new


def test_recover_paused_capture_run_returns_none_when_no_drafts(db):
    # The fixture's run_1 is the only draft; remove it via finalize
    db.promote_draft("run_1", "Finalized")
    assert db.recover_paused_capture_run("smw") is None


def test_recover_paused_capture_run_marks_orphan_sessions_crashed(db):
    db.create_capture_session("sess_1", "run_1", 1, "/tmp/1.spinrec")
    # session_2 is open (orphan)
    db.create_capture_session("sess_2", "run_1", 2, "/tmp/2.spinrec")
    db.end_capture_session("sess_1", end_reason="stopped")
    db.recover_paused_capture_run("smw")
    sess_2 = db.get_capture_session("sess_2")
    assert sess_2["end_reason"] == "crashed"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/db/test_db_capture_sessions.py::test_recover_paused_capture_run_finds_most_recent_draft -v`
Expected: FAIL with `AttributeError`.

- [ ] **Step 3: Implement `recover_paused_capture_run`**

Append to `python/spinlab/db/capture_sessions.py`:

```python
    def recover_paused_capture_run(self, game_id: str) -> str | None:
        """Find the most recent draft (paused) capture_run for the game.

        Side effects:
        - Hard-deletes any older drafts for the same game (defensive — there
          should only be one paused run per game; if there are more, the
          oldest were stranded and are not recoverable into a coherent state).
        - Marks any orphaned open sessions for the recovered run as crashed.

        Returns the recovered run id, or None if no draft exists.
        """
        rows = self.conn.execute(
            "SELECT id FROM capture_runs WHERE game_id = ? AND draft = 1 "
            "ORDER BY created_at DESC",
            (game_id,),
        ).fetchall()
        if not rows:
            return None
        recovered_id = rows[0][0]
        for row in rows[1:]:
            self.hard_delete_capture_run(row[0])
        self.mark_orphan_capture_sessions_crashed(recovered_id)
        return recovered_id
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/db/test_db_capture_sessions.py -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/db/capture_sessions.py tests/unit/db/test_db_capture_sessions.py
git commit -m "feat(db): recover_paused_capture_run with orphan session cleanup"
```

---

## Phase 2: Recorder refactor — write timing to DB on segment close

### Task 9: `SegmentRecorder` writes timing to DB instead of in-memory list

**Files:**
- Modify: `python/spinlab/capture/recorder.py`
- Modify: `python/spinlab/capture/__init__.py`
- Modify: `tests/unit/capture/test_recorder.py`

This task removes `RecordedSegmentTime`, `recorder.segment_times`, `recorder.segments_count`, and `enter_draft()`. `recorder.handle_*` methods now require a `current_capture_session_id` and write directly to DB.

- [ ] **Step 1: Read existing recorder tests to understand the contract**

Run: `cat tests/unit/capture/test_recorder.py | head -120`

Identify the assertions about `segments_count`, `segment_times`, and `enter_draft`. These will need to be replaced with DB queries.

- [ ] **Step 2: Update tests for the new DB-driven contract**

Modify `tests/unit/capture/test_recorder.py` — replace assertions on in-memory `segment_times` with queries to `recorded_segment_times`. Replace `segments_count` checks with `SELECT COUNT(*) FROM segments WHERE reference_id = ?`.

For example, the existing pattern:

```python
assert recorder.segments_count == 1
assert len(recorder.segment_times) == 1
assert recorder.segment_times[0].time_ms == 5000
```

becomes:

```python
seg_count = db.conn.execute(
    "SELECT COUNT(*) FROM segments WHERE reference_id = ?", ("run_1",)
).fetchone()[0]
assert seg_count == 1
times = db.conn.execute(
    "SELECT time_ms FROM recorded_segment_times WHERE capture_session_id = ?",
    ("sess_1",),
).fetchall()
assert [r[0] for r in times] == [5000]
```

The fixture should set up `db.create_capture_run("run_1", ...)` and `db.create_capture_session("sess_1", "run_1", 1, ...)` so the recorder has a valid session id to write against.

The recorder under test must be constructed with `capture_run_id="run_1"` and `current_capture_session_id="sess_1"`.

- [ ] **Step 3: Run updated tests to verify they fail**

Run: `pytest tests/unit/capture/test_recorder.py -v`
Expected: FAIL — `recorder` doesn't accept `current_capture_session_id` and doesn't write to DB.

- [ ] **Step 4: Refactor `SegmentRecorder`**

Replace the contents of `python/spinlab/capture/recorder.py` with:

```python
"""SegmentRecorder — owns reference/replay segment capture state and logic.

Per-segment timing is written directly to `recorded_segment_times` on segment
close. No in-memory accumulation: a dashboard crash mid-run preserves all
captured timing data in the DB.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

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
    type: str              # "entrance" or "checkpoint"
    ordinal: int
    state_path: str | None
    timestamp_ms: int
    level_num: int
    raw_conditions: dict


class SegmentRecorder:
    """Captures segments during reference runs and replays.

    Stateless across recording sessions: created with a `capture_run_id` and a
    `current_capture_session_id`, and writes directly to the DB. Per-session
    boundaries (death counts, spawn timestamps) reset on `clear()`.
    """

    def __init__(self) -> None:
        self.capture_run_id: str | None = None
        self.current_capture_session_id: str | None = None
        self.pending_start: PendingStart | None = None
        self.died: bool = False
        self.rec_path: str | None = None
        self._deaths_in_segment: int = 0
        self._last_spawn_ms: int | None = None

    def clear(self) -> None:
        """Reset per-session state. Does NOT clear DB rows."""
        self.capture_run_id = None
        self.current_capture_session_id = None
        self.pending_start = None
        self.died = False
        self.rec_path = None
        self._deaths_in_segment = 0
        self._last_spawn_ms = None

    def handle_entrance(self, event: LevelEntranceEvent) -> None:
        """Buffer a level entrance as pending start."""
        if self.pending_start and self.pending_start.type != "entrance":
            logger.info("Ignoring level_entrance — pending start exists: %s",
                        self.pending_start)
            return
        self.pending_start = PendingStart(
            type="entrance", ordinal=0,
            state_path=event.state_path, timestamp_ms=event.timestamp_ms,
            level_num=event.level, raw_conditions=event.conditions,
        )
        self.died = False
        self._deaths_in_segment = 0
        self._last_spawn_ms = None

    def _close_segment(self, db, game_id, start: PendingStart, end_type, end_ordinal,
                       level, end_raw_conditions, registry,
                       end_timestamp_ms: int | None = None) -> None:
        """Create waypoints + segment for the segment ending here, persist timing."""
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
        # Compute ordinal from DB count (no in-memory counter)
        existing_count = db.conn.execute(
            "SELECT COUNT(*) FROM segments WHERE reference_id = ?",
            (self.capture_run_id,),
        ).fetchone()[0]
        seg = Segment(
            id=seg_id, game_id=game_id, level_number=level,
            start_type=start.type, start_ordinal=start.ordinal,
            end_type=end_type, end_ordinal=end_ordinal,
            start_waypoint_id=wp_start.id, end_waypoint_id=wp_end.id,
            is_primary=is_primary,
            ordinal=existing_count + 1,
            reference_id=self.capture_run_id,
            capture_session_id=self.current_capture_session_id,
        )
        db.upsert_segment(seg)

        state_path = start.state_path
        if state_path:
            variant = "cold" if start.type == "entrance" else "hot"
            db.add_save_state(WaypointSaveState(
                waypoint_id=wp_start.id,
                variant_type=variant,
                state_path=state_path,
                is_default=True,
            ))

        # Persist timing immediately (crash-safe).
        start_ts = start.timestamp_ms
        if (start_ts is not None and end_timestamp_ms is not None
                and self.current_capture_session_id is not None):
            time_ms = end_timestamp_ms - start_ts
            deaths = self._deaths_in_segment
            if deaths == 0:
                clean_tail_ms = time_ms
            elif self._last_spawn_ms is not None:
                clean_tail_ms = end_timestamp_ms - self._last_spawn_ms
            else:
                clean_tail_ms = time_ms
            db.add_recorded_segment_time(
                self.current_capture_session_id, seg_id,
                time_ms=time_ms, deaths=deaths, clean_tail_ms=clean_tail_ms,
            )

        self._deaths_in_segment = 0
        self._last_spawn_ms = None

    @staticmethod
    def _compute_is_primary(db, game_id, level, start_type, start_ord,
                            end_type, end_ord, new_seg_id) -> bool:
        row = db.conn.execute(
            """SELECT id FROM segments
               WHERE game_id = ? AND level_number = ?
               AND start_type = ? AND start_ordinal = ?
               AND end_type = ? AND end_ordinal = ?
               AND active = 1 AND id != ?""",
            (game_id, level, start_type, start_ord,
             end_type, end_ord, new_seg_id),
        ).fetchone()
        return row is None

    def handle_checkpoint(self, event: CheckpointEvent, game_id: str,
                          db: "Database",
                          registry: "ConditionRegistry") -> None:
        if not self.pending_start:
            return
        cp_ordinal = event.cp_ordinal
        level = event.level_num if event.level_num else self.pending_start.level_num
        self._close_segment(
            db, game_id, self.pending_start, "checkpoint", cp_ordinal,
            level, event.conditions, registry,
            end_timestamp_ms=event.timestamp_ms)
        self.pending_start = PendingStart(
            type="checkpoint", ordinal=cp_ordinal,
            state_path=event.state_path, timestamp_ms=event.timestamp_ms,
            level_num=level, raw_conditions=event.conditions,
        )

    def handle_exit(self, event: LevelExitEvent, game_id: str,
                    db: "Database",
                    registry: "ConditionRegistry") -> None:
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

    def handle_death(self, timestamp_ms: int | None = None) -> None:
        self.died = True
        self._deaths_in_segment += 1

    def handle_spawn_timing(self, timestamp_ms: int | None = None) -> None:
        if timestamp_ms is not None:
            self._last_spawn_ms = timestamp_ms

    def handle_spawn(self, event: SpawnEvent, game_id: str,
                     db: "Database",
                     registry: "ConditionRegistry") -> None:
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

- [ ] **Step 5: Update `__init__.py` exports**

In `python/spinlab/capture/__init__.py`:

```python
"""Capture pipeline: segment recording, draft lifecycle, reference/replay/cold-fill orchestration."""
from .cold_fill import ColdFillController
from .recorder import SegmentRecorder
from .reference import ReferenceController

__all__ = [
    "ColdFillController",
    "ReferenceController",
    "SegmentRecorder",
]
```

(`DraftManager` and `RecordedSegmentTime` removed.)

- [ ] **Step 6: Run recorder tests**

Run: `pytest tests/unit/capture/test_recorder.py -v`
Expected: All PASS. (You may need to iterate on the test updates from Step 2 and the recorder logic from Step 4 until they align.)

- [ ] **Step 7: Commit**

```bash
git add python/spinlab/capture/recorder.py python/spinlab/capture/__init__.py tests/unit/capture/test_recorder.py
git commit -m "refactor(recorder): persist timing to DB; drop in-memory state"
```

Note: at this point `python/spinlab/capture/reference.py` still imports from `recorder` and references `RecordedSegmentTime` / `enter_draft` / `segments_count` / `segment_times` / `DraftManager`. The next tasks fix those.

---

## Phase 3: ReferenceController — multi-session lifecycle

### Task 10: Move `_seed_reference_attempts` into `reference.py` and rewire to drain from DB

**Files:**
- Modify: `python/spinlab/capture/reference.py`
- Delete: `python/spinlab/capture/draft.py`
- Modify: `tests/unit/capture/test_draft.py` → delete
- Test: assertions move to `tests/unit/capture/test_multi_session.py` (next task)

- [ ] **Step 1: Add `_seed_reference_attempts` to `reference.py`**

In `python/spinlab/capture/reference.py`, add this private function near the top of the module (above the class):

```python
from datetime import UTC, datetime as _dt

from ..models import Attempt, AttemptSource


def _seed_reference_attempts(
    db: "Database", capture_run_id: str,
    timing_rows: list[dict],
) -> int:
    """Insert seed attempts from drained recorded_segment_times rows.

    `timing_rows` is the result of `db.drain_recorded_segment_times_for_run(capture_run_id)`.
    Returns count inserted.
    """
    if not timing_rows:
        return 0
    now = _dt.now(UTC)
    count = 0
    for row in timing_rows:
        attempt = Attempt(
            segment_id=row["segment_id"],
            session_id=capture_run_id,  # polymorphic field; reference attempts use the run id
            completed=True,
            time_ms=row["time_ms"],
            deaths=row["deaths"],
            clean_tail_ms=row["clean_tail_ms"],
            source=AttemptSource.REFERENCE,
            created_at=now,
        )
        db.log_attempt(attempt)
        count += 1
        logger.info("seed: segment=%s time=%dms deaths=%d clean_tail=%dms",
                     row["segment_id"], row["time_ms"], row["deaths"],
                     row["clean_tail_ms"])
    return count
```

- [ ] **Step 2: Delete `draft.py`**

```bash
rm python/spinlab/capture/draft.py
```

- [ ] **Step 3: Delete `test_draft.py`** (its coverage moves to test_multi_session.py)

```bash
rm tests/unit/capture/test_draft.py
```

- [ ] **Step 4: Verify nothing references `DraftManager` or `RecordedSegmentTime`**

Run: `rg "DraftManager|RecordedSegmentTime" python/ tests/ -n`

You should see references in:
- `python/spinlab/capture/reference.py` (`self.draft = DraftManager()`, `self.draft.has_draft`, etc.)
- `python/spinlab/session_manager.py` (potentially)
- `python/spinlab/state_builder.py` (the `draft` field in state)

These get fixed in the next task (Task 11 onward). Note them.

- [ ] **Step 5: Don't commit yet** — `reference.py` still has broken `self.draft = DraftManager()` references. This will be fixed in Task 11. Leave the working tree dirty and proceed.

---

### Task 11: `ReferenceController` — refactor state model (RECORDING/PAUSED), add `_end_current_session`

**Files:**
- Modify: `python/spinlab/capture/reference.py`
- Modify: `python/spinlab/errors.py`

This is the big one. We replace the `DraftManager` machinery with explicit RECORDING/PAUSED state, introduce session creation/ending, and consolidate the three duplicate "stop and conditionally promote" code paths into one helper.

- [ ] **Step 1: Add new error class**

In `python/spinlab/errors.py`, add (alongside existing error classes):

```python
class SessionDeleteAfterFinalizeError(ActionError):
    """Cannot delete a capture session after the run has been finalized."""
    code = "session_delete_after_finalize"
    message = "Cannot delete sessions of a finalized run; delete individual segments instead."
```

(Use the same base/style as the other errors in that file.)

- [ ] **Step 2: Rewrite `reference.py` with the new state model**

Replace the entire body of `ReferenceController` in `python/spinlab/capture/reference.py`:

```python
"""ReferenceController — orchestrates reference recording and replay capture.

State model:
- IDLE: no run loaded
- RECORDING: a session is active, recorder is buffering events
- PAUSED: a draft=1 capture_run exists but no active session

Stop is non-destructive: it ends the current session and leaves the run paused.
Resume creates a new session under the existing paused run. Finalize drains
recorded_segment_times into attempts and sets draft=0.
"""
from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from datetime import datetime as _dt
from pathlib import Path
from typing import TYPE_CHECKING

from ..condition_registry import ConditionRegistry
from ..errors import (
    AlreadyReplayingError,
    NoHotVariantError,
    NotConnectedError,
    NotInReferenceError,
    NotReplayingError,
    PracticeActiveError,
    ReferenceActiveError,
    RunPendingError,                # alias of DraftPendingError if you renamed; keep both for compat
    SessionDeleteAfterFinalizeError,
)
from ..models import (
    ActionResult, Attempt, AttemptSource, Mode, Status,
)
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
from .recorder import SegmentRecorder

if TYPE_CHECKING:
    from ..db import Database
    from ..scheduler import Scheduler
    from ..tcp_manager import TcpManager

logger = logging.getLogger(__name__)


def _seed_reference_attempts(
    db: "Database", capture_run_id: str, timing_rows: list[dict],
) -> int:
    """Insert seed attempts from drained recorded_segment_times rows. Returns count."""
    if not timing_rows:
        return 0
    now = _dt.now(UTC)
    count = 0
    for row in timing_rows:
        attempt = Attempt(
            segment_id=row["segment_id"],
            session_id=capture_run_id,
            completed=True,
            time_ms=row["time_ms"],
            deaths=row["deaths"],
            clean_tail_ms=row["clean_tail_ms"],
            source=AttemptSource.REFERENCE,
            created_at=now,
        )
        db.log_attempt(attempt)
        count += 1
        logger.info("seed: segment=%s time=%dms deaths=%d clean_tail=%dms",
                     row["segment_id"], row["time_ms"], row["deaths"],
                     row["clean_tail_ms"])
    return count


class ReferenceController:
    """Manages reference/replay capture, sessions, and finalize/discard."""

    def __init__(self, db: "Database", tcp: "TcpManager") -> None:
        self.db = db
        self.tcp = tcp
        self.recorder = SegmentRecorder()
        self.fill_gap_segment_id: str | None = None
        self._fill_gap_waypoint_id: str | None = None
        self.condition_registry: ConditionRegistry = ConditionRegistry()

        # Paused-run state (set by recovery or by stopping a session)
        self.paused_run_id: str | None = None

    def set_condition_registry(self, registry: ConditionRegistry) -> None:
        self.condition_registry = registry

    @property
    def has_paused_run(self) -> bool:
        return self.paused_run_id is not None

    @property
    def current_capture_session_id(self) -> str | None:
        return self.recorder.current_capture_session_id

    @property
    def rec_path(self) -> str | None:
        return self.recorder.rec_path

    def get_paused_state(self) -> dict | None:
        """Snapshot of the paused run for state_builder. None if no paused run."""
        if not self.paused_run_id:
            return None
        seg_count = self.db.conn.execute(
            "SELECT COUNT(*) FROM segments WHERE reference_id = ? AND active = 1",
            (self.paused_run_id,),
        ).fetchone()[0]
        sessions = self.db.list_capture_sessions_for_run(self.paused_run_id)
        return {
            "run_id": self.paused_run_id,
            "segments_captured": seg_count,
            "session_count": len(sessions),
        }

    def clear_and_idle(self) -> None:
        """Clear all in-memory state. Caller sets mode to IDLE."""
        self.recorder.clear()
        self.paused_run_id = None

    # ---------------------------------------------------------------- helpers

    def _game_rec_dir(self, data_dir: Path, game_id: str) -> Path:
        d = data_dir / game_id / "rec"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _new_session_spinrec_path(
        self, data_dir: Path, game_id: str, run_id: str, ordinal: int,
    ) -> str:
        path = self._game_rec_dir(data_dir, game_id) / f"{run_id}__sess{ordinal:03d}.spinrec"
        return str(path.resolve())

    def _end_current_session(self, end_reason: str) -> None:
        """End the current capture session (if any). Run remains draft=1.

        Called from: stop_reference, handle_disconnect, stop_replay,
        handle_replay_finished, handle_replay_error.
        """
        sess_id = self.recorder.current_capture_session_id
        run_id = self.recorder.capture_run_id
        if sess_id:
            self.db.end_capture_session(sess_id, end_reason=end_reason)
            logger.info("session: ended sess=%s reason=%s", sess_id, end_reason)
        # Surface run as paused (only if we had a run and it's still draft=1)
        if run_id:
            row = self.db.conn.execute(
                "SELECT draft FROM capture_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if row and row[0] == 1:
                self.paused_run_id = run_id
        self.recorder.clear()

    def _create_new_session(self, run_id: str, data_dir: Path, game_id: str) -> tuple[str, str]:
        """Create a new capture_session row + spinrec path. Returns (session_id, spinrec_path)."""
        next_ord = self.db.max_session_ordinal_for_run(run_id) + 1
        sess_id = f"sess_{uuid.uuid4().hex[:8]}"
        spinrec_path = self._new_session_spinrec_path(data_dir, game_id, run_id, next_ord)
        self.db.create_capture_session(
            session_id=sess_id, capture_run_id=run_id,
            ordinal=next_ord, spinrec_path=spinrec_path,
        )
        logger.info("session: created sess=%s run=%s ordinal=%d", sess_id, run_id, next_ord)
        return sess_id, spinrec_path

    # ---------------------------------------------------------------- start/resume

    async def start_reference(
        self, mode: Mode,
        game_id: str, data_dir: Path, run_name: str | None = None,
    ) -> ActionResult:
        if self.paused_run_id:
            raise RunPendingError()
        if mode == Mode.PRACTICE:
            raise PracticeActiveError()
        if mode == Mode.REPLAY:
            raise AlreadyReplayingError()
        if not self.tcp.is_connected:
            raise NotConnectedError()

        self.recorder.clear()
        run_id = f"live_{uuid.uuid4().hex[:8]}"
        run_name = run_name or f"Live {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M')}"
        self.db.create_capture_run(run_id, game_id, run_name, draft=True)
        sess_id, spinrec_path = self._create_new_session(run_id, data_dir, game_id)

        self.recorder.capture_run_id = run_id
        self.recorder.current_capture_session_id = sess_id
        self.paused_run_id = None  # we're now actively recording

        logger.info("reference: started run=%s name=%r", run_id, run_name)
        await self.tcp.send_command(ReferenceStartCmd(path=spinrec_path))
        return ActionResult(status=Status.STARTED, new_mode=Mode.REFERENCE)

    async def resume_reference(
        self, mode: Mode, game_id: str, data_dir: Path,
    ) -> ActionResult:
        if not self.paused_run_id:
            raise NotInReferenceError()  # or a more specific NoPausedRunError
        if mode == Mode.PRACTICE:
            raise PracticeActiveError()
        if mode == Mode.REPLAY:
            raise AlreadyReplayingError()
        if not self.tcp.is_connected:
            raise NotConnectedError()

        run_id = self.paused_run_id
        sess_id, spinrec_path = self._create_new_session(run_id, data_dir, game_id)
        self.recorder.capture_run_id = run_id
        self.recorder.current_capture_session_id = sess_id
        self.paused_run_id = None

        logger.info("reference: resumed run=%s sess=%s", run_id, sess_id)
        await self.tcp.send_command(ReferenceStartCmd(path=spinrec_path))
        return ActionResult(status=Status.STARTED, new_mode=Mode.REFERENCE)

    # ---------------------------------------------------------------- stop/finalize/discard

    async def stop_reference(self, mode: Mode) -> ActionResult:
        if mode != Mode.REFERENCE:
            raise NotInReferenceError()
        if self.tcp.is_connected:
            await self.tcp.send_command(ReferenceStopCmd())
        seg_count_in_run = self.db.conn.execute(
            "SELECT COUNT(*) FROM segments WHERE reference_id = ?",
            (self.recorder.capture_run_id,),
        ).fetchone()[0] if self.recorder.capture_run_id else 0
        logger.info("reference: stopped — %d total segments in run", seg_count_in_run)
        self._end_current_session(end_reason="stopped")
        return ActionResult(status=Status.STOPPED, new_mode=Mode.IDLE)

    async def finalize_run(self, name: str, scheduler: "Scheduler | None" = None) -> ActionResult:
        if not self.paused_run_id:
            raise NotInReferenceError()
        run_id = self.paused_run_id
        timing_rows = self.db.drain_recorded_segment_times_for_run(run_id)
        self.db.promote_draft(run_id, name)
        self.db.set_active_capture_run(run_id)
        seeded = _seed_reference_attempts(self.db, run_id, timing_rows)
        if seeded and scheduler:
            scheduler.rebuild_all_states()
        self.paused_run_id = None
        logger.info("reference: finalized run=%s as %r (seeded %d attempts)",
                     run_id, name, seeded)
        return ActionResult(status=Status.OK)

    async def save_and_finish_run(
        self, mode: Mode, name: str, scheduler: "Scheduler | None" = None,
    ) -> ActionResult:
        """Combined Stop Session + Finalize, atomic. Single-session ergonomics."""
        if mode != Mode.REFERENCE:
            raise NotInReferenceError()
        if self.tcp.is_connected:
            await self.tcp.send_command(ReferenceStopCmd())
        with self.db.transaction():
            self._end_current_session(end_reason="stopped")
            run_id = self.paused_run_id
            if not run_id:
                raise NotInReferenceError()
            timing_rows = self.db.drain_recorded_segment_times_for_run(run_id)
            self.db.promote_draft(run_id, name)
            self.db.set_active_capture_run(run_id)
            seeded = _seed_reference_attempts(self.db, run_id, timing_rows)
        if seeded and scheduler:
            scheduler.rebuild_all_states()
        self.paused_run_id = None
        logger.info("reference: save_and_finish run=%s as %r (seeded %d attempts)",
                     run_id, name, seeded)
        return ActionResult(status=Status.OK, new_mode=Mode.IDLE)

    async def discard_run(self) -> ActionResult:
        if not self.paused_run_id:
            raise NotInReferenceError()
        run_id = self.paused_run_id
        self.db.hard_delete_capture_run(run_id)
        self.paused_run_id = None
        logger.info("reference: discarded run=%s", run_id)
        return ActionResult(status=Status.OK)

    async def delete_capture_session(self, session_id: str) -> ActionResult:
        """Delete a single capture session. Only allowed while run is paused."""
        sess = self.db.get_capture_session(session_id)
        if not sess:
            raise NotInReferenceError()  # or NoSuchSession; reuse for now
        run_id = sess["capture_run_id"]
        # Only allowed if the run is still paused (draft=1)
        row = self.db.conn.execute(
            "SELECT draft FROM capture_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if not row or row[0] != 1:
            raise SessionDeleteAfterFinalizeError()
        # Remove spinrec file from disk
        try:
            Path(sess["spinrec_path"]).unlink(missing_ok=True)
        except OSError:
            pass
        self.db.delete_capture_session(session_id)
        logger.info("session: deleted sess=%s from run=%s", session_id, run_id)
        return ActionResult(status=Status.OK)

    # ---------------------------------------------------------------- replay

    async def start_replay(
        self, mode: Mode,
        game_id: str, spinrec_path: str, speed: int = SPEED_UNCAPPED,
    ) -> ActionResult:
        if self.paused_run_id:
            raise RunPendingError()
        if mode == Mode.PRACTICE:
            raise PracticeActiveError()
        if mode == Mode.REFERENCE:
            raise ReferenceActiveError()
        if mode == Mode.REPLAY:
            raise AlreadyReplayingError()
        if not self.tcp.is_connected:
            raise NotConnectedError()

        # Replay creates its own ephemeral capture_run + session for capture machinery
        self.recorder.clear()
        run_id = f"replay_{uuid.uuid4().hex[:8]}"
        run_name = f"Replay {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M')}"
        self.db.create_capture_run(run_id, game_id, run_name, draft=True)
        # Replays don't need a real spinrec; use a sentinel path
        sess_id = f"sess_{uuid.uuid4().hex[:8]}"
        self.db.create_capture_session(
            session_id=sess_id, capture_run_id=run_id,
            ordinal=1, spinrec_path=spinrec_path,
        )
        self.recorder.capture_run_id = run_id
        self.recorder.current_capture_session_id = sess_id

        await self.tcp.send_command(ReplayCmd(path=spinrec_path, speed=speed))
        return ActionResult(status=Status.STARTED, new_mode=Mode.REPLAY)

    async def stop_replay(self, mode: Mode) -> ActionResult:
        if mode != Mode.REPLAY:
            raise NotReplayingError()
        if self.tcp.is_connected:
            await self.tcp.send_command(ReplayStopCmd())
        run_id = self.recorder.capture_run_id
        seg_count = self.db.conn.execute(
            "SELECT COUNT(*) FROM segments WHERE reference_id = ?", (run_id,),
        ).fetchone()[0] if run_id else 0
        if seg_count > 0:
            self._end_current_session(end_reason="stopped")
        else:
            self._end_current_session(end_reason="stopped")
            if run_id:
                self.db.hard_delete_capture_run(run_id)
                self.paused_run_id = None
        return ActionResult(status=Status.STOPPED, new_mode=Mode.IDLE)

    # ---------------------------------------------------------------- fill_gap (unchanged)

    async def start_fill_gap(self, segment_id: str) -> ActionResult:
        if not self.tcp.is_connected:
            raise NotConnectedError()
        row = self.db.conn.execute(
            "SELECT start_waypoint_id FROM segments WHERE id = ?", (segment_id,)
        ).fetchone()
        start_waypoint_id = row[0] if row else None
        hot = (self.db.get_save_state(start_waypoint_id, "hot")
               if start_waypoint_id else None)
        if not hot:
            raise NoHotVariantError()
        self.fill_gap_segment_id = segment_id
        self._fill_gap_waypoint_id = start_waypoint_id
        await self.tcp.send_command(FillGapLoadCmd(state_path=hot.state_path, message="Die to capture cold start"))
        return ActionResult(status=Status.STARTED, new_mode=Mode.FILL_GAP)

    def handle_fill_gap_spawn(self, event: SpawnEvent) -> bool:
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

    # ---------------------------------------------------------------- event routing

    def handle_entrance(self, event: LevelEntranceEvent) -> None:
        logger.info("capture: entrance level=%s", event.level)
        self.recorder.handle_entrance(event)

    def handle_checkpoint(self, event: CheckpointEvent, game_id: str) -> None:
        logger.info("capture: checkpoint level=%s cp=%s",
                     event.level_num, event.cp_ordinal)
        self.recorder.handle_checkpoint(event, game_id, self.db,
                                           self.condition_registry)

    def handle_death(self, event: DeathEvent) -> None:
        self.recorder.died = True
        self.recorder.handle_death(timestamp_ms=None)

    def handle_spawn(self, event: SpawnEvent, game_id: str) -> None:
        logger.info("capture: spawn level=%s state_captured=%s",
                     event.level_num, event.state_captured)
        self.recorder.handle_spawn_timing(timestamp_ms=None)
        self.recorder.handle_spawn(event, game_id, self.db,
                                      self.condition_registry)

    def handle_exit(self, event: LevelExitEvent, game_id: str) -> None:
        logger.info("capture: exit level=%s", event.level)
        self.recorder.handle_exit(event, game_id, self.db,
                                     self.condition_registry)

    def handle_rec_saved(self, event: RecSavedEvent) -> None:
        self.recorder.rec_path = event.path

    def handle_replay_finished(self) -> None:
        self._end_current_session(end_reason="stopped")

    def handle_replay_error(self) -> None:
        run_id = self.recorder.capture_run_id
        seg_count = self.db.conn.execute(
            "SELECT COUNT(*) FROM segments WHERE reference_id = ?", (run_id,),
        ).fetchone()[0] if run_id else 0
        self._end_current_session(end_reason="replay_error")
        if seg_count == 0 and run_id:
            self.db.hard_delete_capture_run(run_id)
            self.paused_run_id = None

    def handle_disconnect(self) -> None:
        """Treat as a clean session end. Run stays paused for resume."""
        self._end_current_session(end_reason="disconnected")

    # ---------------------------------------------------------------- recovery

    def recover_paused_run(self, game_id: str) -> None:
        """On game-load, find any paused run for this game and surface it."""
        run_id = self.db.recover_paused_capture_run(game_id)
        self.paused_run_id = run_id
        if run_id:
            logger.info("recovery: paused run loaded id=%s", run_id)
```

- [ ] **Step 3: Add `RunPendingError` to errors.py**

In `python/spinlab/errors.py`, add an alias (or rename `DraftPendingError` if all callers are within this codebase):

```python
class RunPendingError(ActionError):
    """A reference run is already in progress or paused."""
    code = "run_pending"
    message = "Finalize or discard the current run before starting a new one."
```

For backwards compat with any tests that still reference `DraftPendingError`, also add:

```python
DraftPendingError = RunPendingError  # legacy alias
```

- [ ] **Step 4: Verify the file imports cleanly**

Run: `python -c "from spinlab.capture.reference import ReferenceController; print('ok')"`
Expected: `ok`. If you see ImportError, fix the imports listed at the top of `reference.py`.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/capture/reference.py python/spinlab/capture/draft.py python/spinlab/capture/__init__.py python/spinlab/errors.py tests/unit/capture/test_draft.py
git commit -m "refactor(reference): multi-session lifecycle + dissolve DraftManager"
```

---

### Task 12: New unit tests for multi-session lifecycle

**Files:**
- Create: `tests/unit/capture/test_multi_session.py`
- Modify: `tests/factories.py`

- [ ] **Step 1: Add factory helper**

In `tests/factories.py`, add:

```python
def make_capture_session(db, run_id, ordinal=1, session_id=None, spinrec_path=None):
    import uuid
    sid = session_id or f"sess_{uuid.uuid4().hex[:8]}"
    path = spinrec_path or f"/tmp/{sid}.spinrec"
    db.create_capture_session(sid, run_id, ordinal, path)
    return sid
```

- [ ] **Step 2: Write the multi-session lifecycle test**

Create `tests/unit/capture/test_multi_session.py`:

```python
"""Multi-session reference run lifecycle tests."""
from pathlib import Path

import pytest
import pytest_asyncio

from spinlab.capture import ReferenceController
from spinlab.db import Database
from spinlab.errors import RunPendingError, SessionDeleteAfterFinalizeError
from spinlab.models import Mode, Status

from tests.fakes import FakeTcpManager  # adjust import to wherever fake TCP lives


@pytest.fixture
def db(tmp_path):
    d = Database(":memory:")
    d.upsert_game("smw", "Super Mario World", "any%")
    yield d
    d.close()


@pytest.fixture
def tcp():
    return FakeTcpManager(connected=True)


@pytest.fixture
def controller(db, tcp):
    return ReferenceController(db, tcp)


@pytest_asyncio.fixture
async def started_session(controller, db, tmp_path):
    """A controller in RECORDING with an open session under a fresh run."""
    result = await controller.start_reference(
        Mode.IDLE, "smw", tmp_path, run_name="Test Run",
    )
    assert result.new_mode == Mode.REFERENCE
    return controller


# --- Single-session save_and_finish path ---

@pytest.mark.asyncio
async def test_save_and_finish_seeds_attempts_and_finalizes(started_session, db):
    # Simulate one segment captured + timing recorded
    sess_id = started_session.recorder.current_capture_session_id
    run_id = started_session.recorder.capture_run_id
    db.add_recorded_segment_time(sess_id, "seg_x", time_ms=1500, deaths=0, clean_tail_ms=1500)
    # Need a real segment row so the seeded attempt has a valid FK
    _make_minimal_segment(db, run_id, sess_id, "seg_x")

    result = await started_session.save_and_finish_run(Mode.REFERENCE, name="My Run")

    assert result.status == Status.OK
    assert result.new_mode == Mode.IDLE
    # Run is finalized
    row = db.conn.execute("SELECT draft, name FROM capture_runs WHERE id = ?", (run_id,)).fetchone()
    assert row[0] == 0
    assert row[1] == "My Run"
    # Attempt seeded
    attempts = db.conn.execute(
        "SELECT segment_id, time_ms FROM attempts WHERE segment_id = 'seg_x'"
    ).fetchall()
    assert [(r[0], r[1]) for r in attempts] == [("seg_x", 1500)]
    # Buffer drained
    rows = db.conn.execute(
        "SELECT COUNT(*) FROM recorded_segment_times "
        "WHERE capture_session_id = ?", (sess_id,)
    ).fetchone()
    assert rows[0] == 0


# --- Multi-session: stop then resume ---

@pytest.mark.asyncio
async def test_stop_session_pauses_run(started_session, db):
    run_id = started_session.recorder.capture_run_id
    result = await started_session.stop_reference(Mode.REFERENCE)
    assert result.new_mode == Mode.IDLE
    assert started_session.paused_run_id == run_id
    sessions = db.list_capture_sessions_for_run(run_id)
    assert len(sessions) == 1
    assert sessions[0]["end_reason"] == "stopped"
    # Run still draft=1
    draft = db.conn.execute(
        "SELECT draft FROM capture_runs WHERE id = ?", (run_id,)
    ).fetchone()[0]
    assert draft == 1


@pytest.mark.asyncio
async def test_resume_creates_new_session_under_same_run(started_session, db, tmp_path):
    run_id = started_session.recorder.capture_run_id
    await started_session.stop_reference(Mode.REFERENCE)

    result = await started_session.resume_reference(Mode.IDLE, "smw", tmp_path)
    assert result.new_mode == Mode.REFERENCE
    assert started_session.recorder.capture_run_id == run_id
    assert started_session.paused_run_id is None
    sessions = db.list_capture_sessions_for_run(run_id)
    assert [s["ordinal"] for s in sessions] == [1, 2]
    assert sessions[1]["ended_at"] is None


# --- Discard ---

@pytest.mark.asyncio
async def test_discard_run_hard_deletes_everything(started_session, db):
    run_id = started_session.recorder.capture_run_id
    sess_id = started_session.recorder.current_capture_session_id
    db.add_recorded_segment_time(sess_id, "seg_x", time_ms=100, deaths=0, clean_tail_ms=100)
    await started_session.stop_reference(Mode.REFERENCE)

    result = await started_session.discard_run()
    assert result.status == Status.OK
    assert started_session.paused_run_id is None
    assert db.list_capture_sessions_for_run(run_id) == []
    rows = db.conn.execute("SELECT COUNT(*) FROM capture_runs WHERE id = ?", (run_id,)).fetchone()
    assert rows[0] == 0


# --- Delete session ---

@pytest.mark.asyncio
async def test_delete_capture_session_while_paused(started_session, db, tmp_path):
    run_id = started_session.recorder.capture_run_id
    sess_id = started_session.recorder.current_capture_session_id
    await started_session.stop_reference(Mode.REFERENCE)
    # Resume to create session 2
    await started_session.resume_reference(Mode.IDLE, "smw", tmp_path)
    sess_2 = started_session.recorder.current_capture_session_id
    await started_session.stop_reference(Mode.REFERENCE)
    # Delete session 1
    result = await started_session.delete_capture_session(sess_id)
    assert result.status == Status.OK
    sessions = db.list_capture_sessions_for_run(run_id)
    assert {s["id"] for s in sessions} == {sess_2}


@pytest.mark.asyncio
async def test_delete_capture_session_after_finalize_rejected(started_session, db):
    run_id = started_session.recorder.capture_run_id
    sess_id = started_session.recorder.current_capture_session_id
    await started_session.save_and_finish_run(Mode.REFERENCE, name="Done")
    with pytest.raises(SessionDeleteAfterFinalizeError):
        await started_session.delete_capture_session(sess_id)


# --- One paused run per game ---

@pytest.mark.asyncio
async def test_start_reference_rejects_when_paused_run_exists(started_session, tmp_path):
    await started_session.stop_reference(Mode.REFERENCE)
    with pytest.raises(RunPendingError):
        await started_session.start_reference(
            Mode.IDLE, "smw", tmp_path, run_name="Other",
        )


# --- Disconnect ---

@pytest.mark.asyncio
async def test_disconnect_pauses_run(started_session, db):
    run_id = started_session.recorder.capture_run_id
    started_session.handle_disconnect()
    assert started_session.paused_run_id == run_id
    sessions = db.list_capture_sessions_for_run(run_id)
    assert sessions[0]["end_reason"] == "disconnected"


# --- Helpers ---

def _make_minimal_segment(db, run_id, sess_id, seg_id):
    """Insert a minimal valid segment row for FK referential integrity."""
    from spinlab.models import EndpointType, Segment, Waypoint
    wp_a = Waypoint.make("smw", 1, EndpointType.ENTRANCE, 0, {})
    wp_b = Waypoint.make("smw", 1, EndpointType.GOAL, 0, {})
    db.upsert_waypoint(wp_a)
    db.upsert_waypoint(wp_b)
    seg = Segment(
        id=seg_id, game_id="smw", level_number=1,
        start_type=EndpointType.ENTRANCE, start_ordinal=0,
        end_type=EndpointType.GOAL, end_ordinal=0,
        start_waypoint_id=wp_a.id, end_waypoint_id=wp_b.id,
        reference_id=run_id, capture_session_id=sess_id,
    )
    db.upsert_segment(seg)
```

- [ ] **Step 3: Locate the fake TCP manager**

Run: `rg "FakeTcpManager|class Fake.*Tcp" tests/ -n`

Update the import in `test_multi_session.py` to match the actual location.

- [ ] **Step 4: Run multi-session tests**

Run: `pytest tests/unit/capture/test_multi_session.py -v`
Expected: All PASS. Iterate on any test that doesn't match the controller's behavior — likely either fix the test (if I misread the API I designed) or fix the controller.

- [ ] **Step 5: Commit**

```bash
git add tests/unit/capture/test_multi_session.py tests/factories.py
git commit -m "test: multi-session reference lifecycle (start, stop, resume, finalize, discard, delete-session)"
```

---

### Task 13: Update existing reference tests for new disconnect behavior

**Files:**
- Modify: `tests/unit/capture/test_reference.py`

- [ ] **Step 1: Identify failing tests**

Run: `pytest tests/unit/capture/test_reference.py -v`

Expect failures around:
- Tests asserting `handle_disconnect` enters draft state (now: pauses)
- Tests using `DraftManager` API directly (now removed)
- Tests using `recorder.segments_count` or `recorder.segment_times` (now removed)
- Tests using `enter_draft()` (now removed)

- [ ] **Step 2: For each failing test**

For each, decide:
- If the test asserted the OLD behavior of "disconnect promotes to draft" — rewrite the assertion to check `controller.paused_run_id` and `db.list_capture_sessions_for_run(...)` instead.
- If the test exercised `DraftManager.save/discard/recover` directly — replace with the corresponding `ReferenceController.finalize_run/discard_run/recover_paused_run`.
- If the test depended on in-memory `segments_count` — query the DB.

There's no shortcut here; read each test and update.

- [ ] **Step 3: Run all capture tests**

Run: `pytest tests/unit/capture/ -v`
Expected: All PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/unit/capture/test_reference.py
git commit -m "test(reference): update for multi-session disconnect/pause behavior"
```

---

## Phase 4: SessionManager + routes

### Task 14: Update `SessionManager` to expose new actions

**Files:**
- Modify: `python/spinlab/session_manager.py`
- Modify: `python/spinlab/state_builder.py`
- Modify: `tests/unit/test_session_manager.py`

- [ ] **Step 1: Replace `save_draft` and `discard_draft` proxies with new methods**

In `python/spinlab/session_manager.py`, replace lines around 408-421 (the `save_draft` / `discard_draft` methods) with:

```python
    async def finalize_run(self, name: str) -> ActionResult:
        scheduler = self.get_scheduler() if self.game_id else None
        result = await self.capture.finalize_run(name, scheduler=scheduler)
        if result.status == Status.OK and self.game_id and self.tcp.is_connected:
            cf_result = await self.cold_fill.start(self.game_id)
            if cf_result.new_mode == Mode.COLD_FILL:
                self.mode = Mode.COLD_FILL
        await self._notify_sse()
        return result

    async def save_and_finish_run(self, name: str) -> ActionResult:
        scheduler = self.get_scheduler() if self.game_id else None
        result = await self.capture.save_and_finish_run(self.mode, name, scheduler=scheduler)
        if result.new_mode is not None:
            self.mode = result.new_mode
        if result.status == Status.OK and self.game_id and self.tcp.is_connected:
            cf_result = await self.cold_fill.start(self.game_id)
            if cf_result.new_mode == Mode.COLD_FILL:
                self.mode = Mode.COLD_FILL
        await self._notify_sse()
        return result

    async def discard_run(self) -> ActionResult:
        result = await self.capture.discard_run()
        await self._notify_sse()
        return result

    async def resume_reference(self) -> ActionResult:
        return await self._apply_result(
            await self.capture.resume_reference(
                self.mode, self.require_game(), self.data_dir,
            )
        )

    async def delete_capture_session(self, session_id: str) -> ActionResult:
        result = await self.capture.delete_capture_session(session_id)
        await self._notify_sse()
        return result
```

- [ ] **Step 2: Update `recover_draft` call to `recover_paused_run`**

Find the existing call (around line 209):

```python
        self.capture.recover_draft(game_id)
```

Replace with:

```python
        self.capture.recover_paused_run(game_id)
```

- [ ] **Step 3: Update any references to `self.capture.has_draft`**

Run: `rg "has_draft" python/spinlab/ -n`

Replace each with `self.capture.has_paused_run`. (Likely in `start_practice` and similar guards.)

- [ ] **Step 4: Update state_builder for new state shape**

In `python/spinlab/state_builder.py`, find where `draft` was added to the state dict. Replace with `paused_run`:

```python
paused_run = self.session.capture.get_paused_state()
state["paused_run"] = paused_run
```

(Remove the old `draft` field entirely; the frontend will be updated to read `paused_run`.)

- [ ] **Step 5: Update existing session_manager tests**

Run: `pytest tests/unit/test_session_manager.py -v`

Failures will reference removed `save_draft`/`discard_draft` proxies. Update tests to call the new methods (`finalize_run`, `save_and_finish_run`, `discard_run`).

- [ ] **Step 6: Run all unit tests**

Run: `pytest -m "not (emulator or slow or frontend)" -v`
Expected: All PASS. (This is the fast suite — ~23s.)

- [ ] **Step 7: Commit**

```bash
git add python/spinlab/session_manager.py python/spinlab/state_builder.py tests/unit/test_session_manager.py
git commit -m "feat(session_manager): expose finalize/save_and_finish/discard/resume/delete_session"
```

---

### Task 15: Update routes — rename old, add new

**Files:**
- Modify: `python/spinlab/routes/reference.py`
- Test: `tests/unit/routes/test_dashboard_references.py` (existing) + likely additions

- [ ] **Step 1: Rewrite the routes file**

Replace the relevant sections of `python/spinlab/routes/reference.py`:

```python
@router.post("/reference/finalize")
async def reference_finalize(req: Request, session: SessionManager = Depends(get_session)):
    body = await req.json()
    name = body.get("name", "Untitled")
    return (await session.finalize_run(name)).to_response()


@router.post("/reference/save_and_finish")
async def reference_save_and_finish(req: Request, session: SessionManager = Depends(get_session)):
    body = await req.json()
    name = body.get("name", "Untitled")
    return (await session.save_and_finish_run(name)).to_response()


@router.post("/reference/discard_run")
async def reference_discard_run(session: SessionManager = Depends(get_session)):
    return (await session.discard_run()).to_response()


@router.post("/reference/resume")
async def reference_resume(session: SessionManager = Depends(get_session)):
    return (await session.resume_reference()).to_response()


@router.delete("/capture_sessions/{session_id}")
async def delete_capture_session(session_id: str, session: SessionManager = Depends(get_session)):
    return (await session.delete_capture_session(session_id)).to_response()


@router.get("/capture_sessions")
def list_capture_sessions(
    run_id: str,
    session: SessionManager = Depends(get_session),
    db: Database = Depends(get_db),
):
    return {"sessions": db.list_capture_sessions_for_run(run_id)}
```

Remove the old `/references/draft/save` and `/references/draft/discard` routes.

- [ ] **Step 2: Find and update any test that hits the old routes**

Run: `rg "draft/save|draft/discard" tests/ -n`

Update each to call the new endpoint name.

- [ ] **Step 3: Add tests for new routes**

Append to `tests/unit/routes/test_dashboard_references.py`:

```python
def test_reference_finalize_route_exists(client_with_paused_run):
    # Arrange: client_with_paused_run fixture should set up a paused run
    response = client_with_paused_run.post(
        "/api/reference/finalize", json={"name": "My Run"}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_reference_resume_route_starts_new_session(client_with_paused_run):
    response = client_with_paused_run.post("/api/reference/resume")
    assert response.status_code == 200
    assert response.json()["status"] == "started"


def test_delete_capture_session_route(client_with_paused_run, db_with_paused_run):
    sessions = db_with_paused_run.list_capture_sessions_for_run("run_test")
    sess_id = sessions[0]["id"]
    response = client_with_paused_run.delete(f"/api/capture_sessions/{sess_id}")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
```

(Build `client_with_paused_run` and `db_with_paused_run` fixtures by following the patterns of existing fixtures in the file.)

- [ ] **Step 4: Run route tests**

Run: `pytest tests/unit/routes/test_dashboard_references.py -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/routes/reference.py tests/unit/routes/test_dashboard_references.py
git commit -m "feat(routes): finalize/save_and_finish/discard_run/resume/delete_capture_session"
```

---

## Phase 5: Lua cleanup

### Task 16: Remove `MAX_RECORDING_FRAMES` from spinlab.lua

**Files:**
- Modify: `lua/spinlab.lua`

- [ ] **Step 1: Remove the constant**

In `lua/spinlab.lua`, remove line 25:

```lua
local MAX_RECORDING_FRAMES = 360000  -- 100 minutes at 60fps
```

- [ ] **Step 2: Remove the cap-hit branch in `on_input_polled`**

Find the block in `on_input_polled` (around lines 1276-1287):

```lua
    if recording.frame_index >= MAX_RECORDING_FRAMES then
      log("WARNING: Recording hit MAX_RECORDING_FRAMES (" .. MAX_RECORDING_FRAMES .. "), auto-stopping")
      local path = recording.output_path
      local count = #recording.buffer
      if count > 0 and path then
        flush_spinrec(path, game_id, recording.buffer)
        send_event({event = "rec_saved", path = path, frame_count = count})
      end
      recording.active = false
      recording.buffer = {}
      recording.frame_index = 0
      recording.output_path = nil
    end
```

Delete it entirely. The surrounding `if recording.active then ... end` block stays.

- [ ] **Step 3: Run emulator tests**

Run: `pytest -m emulator -v`
Expected: All PASS. Recording with no cap should still flush correctly on `reference_stop`.

- [ ] **Step 4: Commit**

```bash
git add lua/spinlab.lua
git commit -m "feat(lua): remove MAX_RECORDING_FRAMES (sessions are user-driven now)"
```

---

## Phase 6: Frontend

### Task 17: Update TypeScript types

**Files:**
- Modify: `frontend/src/types.ts`

- [ ] **Step 1: Add `CaptureSession` and update `AppState`**

In `frontend/src/types.ts`, find the `AppState` type and similar. Add:

```typescript
export interface CaptureSession {
  id: string;
  capture_run_id: string;
  ordinal: number;
  started_at: string;
  ended_at: string | null;
  spinrec_path: string;
  end_reason: string | null;
}

export interface PausedRunState {
  run_id: string;
  segments_captured: number;
  session_count: number;
}
```

In `AppState`, replace any existing `draft?: { ... }` field with:

```typescript
paused_run?: PausedRunState | null;
```

In any segment-shaped type used by the manage page, add:

```typescript
capture_session_id?: string | null;
```

- [ ] **Step 2: Verify TypeScript compiles**

Run: `cd frontend && npm run typecheck`
Expected: No errors. (If errors appear in `manage.ts`, that's expected — fixed in next task.)

- [ ] **Step 3: Don't commit yet** — `manage.ts` will be updated in Task 18, batched commit after that.

---

### Task 18: Rework `manage.ts` to show paused run with sessions sublist

**Files:**
- Modify: `frontend/src/manage.ts`
- Modify: `frontend/index.html`
- Modify: `frontend/style.css`

This is a significant frontend rewrite. The existing `draftPrompt` (a banner saying "Captured X segments — save or discard?") becomes a paused-run card with the new buttons and a sessions sublist.

- [ ] **Step 1: Update `index.html`**

Locate the existing `draft-prompt` element. Replace the surrounding HTML for the reference panel with:

```html
<div id="paused-run-card" style="display:none">
  <h3>Run paused</h3>
  <div id="paused-run-summary"></div>
  <button id="btn-save-and-finish">Save &amp; Finish Run</button>
  <button id="btn-resume">Resume</button>
  <button id="btn-discard-run">Discard</button>
  <details>
    <summary>Sessions</summary>
    <table id="sessions-table">
      <thead><tr><th>#</th><th>Started</th><th>Ended</th><th>Reason</th><th>Segs</th><th></th></tr></thead>
      <tbody id="sessions-body"></tbody>
    </table>
  </details>
</div>
```

(Adapt to existing styling/structure.)

- [ ] **Step 2: Rewrite the manage.ts logic for the paused-run card**

In `frontend/src/manage.ts`, replace the `hasDraft`/`draftPrompt` block around lines 41-57 with logic for `paused_run`:

```typescript
const pausedRun = lastState?.paused_run || null;
const pausedRunCard = document.getElementById("paused-run-card") as HTMLElement;
const recording = lastState?.mode === "reference";

if (pausedRun) {
  pausedRunCard.style.display = "";
  document.getElementById("paused-run-summary")!.textContent =
    `${pausedRun.segments_captured} segments captured across ${pausedRun.session_count} sessions`;
  await renderSessionsList(pausedRun.run_id);
} else {
  pausedRunCard.style.display = "none";
}

(document.getElementById("btn-resume") as HTMLButtonElement).disabled =
  !pausedRun || recording || !lastState?.tcp_connected;
(document.getElementById("btn-save-and-finish") as HTMLButtonElement).disabled =
  !pausedRun;
(document.getElementById("btn-discard-run") as HTMLButtonElement).disabled =
  !pausedRun;
```

Replace previous `hasDraft` checks throughout with `pausedRun != null`.

- [ ] **Step 3: Add `renderSessionsList`**

Add to `manage.ts`:

```typescript
async function renderSessionsList(runId: string): Promise<void> {
  const data = await fetchJSON<{ sessions: CaptureSession[] }>(
    `/api/capture_sessions?run_id=${encodeURIComponent(runId)}`,
  );
  const body = document.getElementById("sessions-body")!;
  body.innerHTML = "";
  (data?.sessions || []).forEach((s) => {
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td>${s.ordinal}</td>` +
      `<td>${s.started_at}</td>` +
      `<td>${s.ended_at || "(open)"}</td>` +
      `<td>${s.end_reason || ""}</td>` +
      `<td>0</td>` +
      `<td><button data-sess-id="${s.id}" class="btn-del-sess">Delete</button></td>`;
    body.appendChild(tr);
  });
  body.querySelectorAll(".btn-del-sess").forEach((btn) =>
    btn.addEventListener("click", async (e) => {
      const sid = (e.currentTarget as HTMLElement).getAttribute("data-sess-id")!;
      if (!confirm("Delete this session and its segments?")) return;
      await postJSON(`/api/capture_sessions/${sid}`, {}, "DELETE");
      await fetchManage();
    }),
  );
}
```

(Adjust `postJSON` signature to support DELETE if it doesn't already.)

- [ ] **Step 4: Wire button handlers**

Add (or update) handlers for the three new buttons:

```typescript
document.getElementById("btn-resume")?.addEventListener("click", async () => {
  await postJSON("/api/reference/resume", {});
});
document.getElementById("btn-save-and-finish")?.addEventListener("click", async () => {
  const name = prompt("Name for this reference run?");
  if (!name) return;
  await postJSON("/api/reference/save_and_finish", { name });
});
document.getElementById("btn-discard-run")?.addEventListener("click", async () => {
  if (!confirm("Discard this run? All sessions and segments will be deleted.")) return;
  await postJSON("/api/reference/discard_run", {});
});
```

- [ ] **Step 5: Build the frontend**

Run: `cd frontend && npm run build`
Expected: Build succeeds.

- [ ] **Step 6: Run frontend tests**

Run: `cd frontend && npm test`
Expected: All PASS.

- [ ] **Step 7: Run frontend smoke tests**

Run: `pytest -m frontend -v`
Expected: All PASS. (May require updating `tests/unit/frontend/api-contract.test.ts` to match new field names.)

- [ ] **Step 8: Commit**

```bash
git add frontend/src/types.ts frontend/src/manage.ts frontend/index.html frontend/style.css
git commit -m "feat(frontend): paused-run card with sessions sublist"
```

---

## Phase 7: Integration tests

### Task 19: Crash-and-recover Python integration test

**Files:**
- Create: `tests/integration/test_crash_recovery.py`

- [ ] **Step 1: Write the test**

Create `tests/integration/test_crash_recovery.py`:

```python
"""Crash-and-recover for multi-session reference runs.

Simulates: dashboard process dies mid-recording. On restart with same DB:
- Orphaned session marked end_reason=crashed
- Run remains draft=1
- Segments and recorded_segment_times preserved
- Resume creates a new session ordinal+1
"""
import pytest
import pytest_asyncio
from pathlib import Path

from spinlab.capture import ReferenceController
from spinlab.db import Database
from spinlab.models import Mode

from tests.fakes import FakeTcpManager


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "test.db"


@pytest.fixture
def db(db_path):
    d = Database(str(db_path))
    d.upsert_game("smw", "Super Mario World", "any%")
    yield d
    d.close()


@pytest.mark.asyncio
async def test_dashboard_crash_mid_session_recovers(db, db_path, tmp_path):
    # --- Pre-crash: start a run, capture some timing, die without graceful shutdown ---
    tcp = FakeTcpManager(connected=True)
    controller = ReferenceController(db, tcp)
    await controller.start_reference(Mode.IDLE, "smw", tmp_path, run_name="Long Run")
    run_id = controller.recorder.capture_run_id
    sess_id_1 = controller.recorder.current_capture_session_id
    db.add_recorded_segment_time(sess_id_1, "seg_a", time_ms=1000, deaths=0, clean_tail_ms=1000)

    # Simulate crash: drop the controller and DB references without ending the session
    del controller
    db.close()

    # --- Post-crash: new dashboard instance, same DB file ---
    db2 = Database(str(db_path))
    tcp2 = FakeTcpManager(connected=True)
    controller2 = ReferenceController(db2, tcp2)
    controller2.recover_paused_run("smw")

    assert controller2.paused_run_id == run_id
    sessions = db2.list_capture_sessions_for_run(run_id)
    assert len(sessions) == 1
    assert sessions[0]["end_reason"] == "crashed"
    times = db2.conn.execute(
        "SELECT segment_id, time_ms FROM recorded_segment_times "
        "WHERE capture_session_id = ?", (sess_id_1,),
    ).fetchall()
    assert [(r[0], r[1]) for r in times] == [("seg_a", 1000)]

    # --- Resume creates session 2 ---
    await controller2.resume_reference(Mode.IDLE, "smw", tmp_path)
    sess_id_2 = controller2.recorder.current_capture_session_id
    assert sess_id_2 != sess_id_1
    sessions = db2.list_capture_sessions_for_run(run_id)
    assert [s["ordinal"] for s in sessions] == [1, 2]
    db2.close()
```

- [ ] **Step 2: Run the test**

Run: `pytest tests/integration/test_crash_recovery.py -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_crash_recovery.py
git commit -m "test: crash-and-recover for multi-session reference runs"
```

---

### Task 20: Playwright smoke for resume after dashboard restart

**Files:**
- Create: `tests/integration/test_multi_session_smoke.py`

- [ ] **Step 1: Read the existing smoke test for setup patterns**

Run: `cat tests/integration/test_frontend_smoke.py`

Note: how the dashboard is launched, how Playwright connects, the static-asset routing pattern.

- [ ] **Step 2: Write the smoke test**

Create `tests/integration/test_multi_session_smoke.py`:

```python
"""Playwright smoke: dashboard crash + restart + resume preserves segments."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.frontend

# This test requires:
# - Built frontend assets (`cd frontend && npm run build`)
# - A fake TCP manager so we can fabricate segment events without Mesen
#
# Pattern: spin up the dashboard against a tmp_path DB, drive a fake
# reference start + segment via FakeTcpManager, kill the process, restart,
# verify the UI shows the paused run with the captured segment.


@pytest.mark.asyncio
async def test_resume_after_dashboard_restart(tmp_path):
    """High-visibility one-shot: paused run survives dashboard restart."""
    # The implementation uses the same fixture pattern as test_frontend_smoke.py.
    # The Python integration test (test_crash_recovery.py) covers the data layer
    # exhaustively; this test exists to verify the UI surfaces the paused state
    # correctly to the user.
    #
    # Steps:
    # 1. Start dashboard #1 against tmp_path DB. Use Playwright to navigate to /.
    # 2. Drive a reference start via FakeTcp; fabricate one segment-close event.
    # 3. Tear down dashboard #1 without graceful shutdown.
    # 4. Start dashboard #2 against the same DB.
    # 5. Navigate to /. Assert the paused-run card is visible with "1 segments captured across 1 sessions".
    # 6. Click Resume. Assert mode transitions to recording.
    pytest.skip("Implementation requires extending the existing smoke fixture; see test_crash_recovery.py for data-layer assertions")
```

The skip is intentional — this test is the visible integration story but writing the full fixture chain is non-trivial. The Python crash test in Task 19 is the load-bearing assertion. Mark this as a follow-up if the fixture refactor proves heavy.

- [ ] **Step 3: Commit (with the skip)**

```bash
git add tests/integration/test_multi_session_smoke.py
git commit -m "test(smoke): scaffold for Playwright crash-and-resume (skipped pending fixture)"
```

(If you have time/energy to fully implement the Playwright test, do so before committing — but don't block merge on it.)

---

## Phase 8: Validation

### Task 21: Full test suite + manual smoke

- [ ] **Step 1: Run the full test suite from CLAUDE.md**

Run: `python -m pytest`
Expected: All PASS. Per `CLAUDE.md`: "Fix all failures, even pre-existing ones unrelated to your current work. A red suite is never acceptable."

- [ ] **Step 2: Type check**

Run: `npx pyright python/`
Expected: No new errors introduced. (Pre-existing errors don't block per CLAUDE.md but should be noted.)

- [ ] **Step 3: Lint**

Run: `ruff check python/`
Expected: No new errors. `ruff check --fix python/` for safe auto-fixes.

- [ ] **Step 4: Frontend type check**

Run: `cd frontend && npm run typecheck`
Expected: No errors.

- [ ] **Step 5: Manual smoke (if reachable)**

If you can launch the dashboard manually:

1. `spinlab db reset` and `spinlab dashboard`
2. Load a game with a ROM available.
3. Start a reference run, capture a segment or two.
4. Click Stop Session — verify the paused-run card appears with [Save & Finish] [Resume] [Discard].
5. Click Resume, capture another segment, Stop Session.
6. Verify session count = 2, segment count is correct.
7. Click Save & Finish, name it, verify it appears in the references list.
8. Repeat from step 3 but click Discard at the end — verify everything is gone.

- [ ] **Step 6: Final commit if anything was tweaked**

If any small fixes came out of validation, commit them.

```bash
git status
git diff
git add ...
git commit -m "fix: ..."
```

---

## Notes for the implementer

- **Greenfield DB:** the existing `_init_schema` machinery in `db/core.py` will rebuild any table whose column set drifted. New tables in `SCHEMA` get created. For `segments`, updating `_expected_columns` triggers the rebuild that picks up the new `capture_session_id` column. No migration needed.
- **`attempts.session_id` is polymorphic.** It stores a practice session id for practice attempts, a speed_run session id for speed_run attempts, and a `capture_run_id` for reference attempts. Do **not** try to rename it. The new `capture_session_id` column on `segments` is unrelated.
- **One paused run per game.** If `recover_paused_capture_run` finds multiple drafts for a game, it keeps the most recent and hard-deletes the rest. This is defensive — there shouldn't be multiple — but mirrors the prior `recover_draft` behavior.
- **Replay still works on a single spinrec.** The replay flow creates an ephemeral capture_run + capture_session for its own bookkeeping, but uses the spinrec path passed in directly. Multi-session replay was rejected as out of scope.
- **Use small, focused commits.** The plan above commits after each task. Each commit should pass `pytest -m "not (emulator or slow or frontend)"` (the fast suite). Run that suite after each commit if anything feels iffy.
- **The `_end_current_session` helper consolidates three duplicate paths.** That refactor is small but valuable — verify after Task 11 that `stop_replay`, `handle_replay_finished`, `handle_replay_error`, `handle_disconnect`, and `stop_reference` all call it (or extend it cleanly).
