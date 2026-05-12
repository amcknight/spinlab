# Bundle 1: Database Encapsulation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Push schema knowledge out of capture controllers, state_builder, and routes by promoting ad-hoc `db.conn.execute(...)` calls to named Database methods; convert SegmentRecorder's per-call `db` + `registry` parameters to constructor injection.

**Architecture:** Two related refactors. (A1) Audit every `db.conn.execute` outside the `db/` package, group by query shape, add a focused method to the appropriate Database mixin with a TDD unit test, then migrate each callsite. (C5) `SegmentRecorder.__init__` takes `db` and a mutable `condition_registry` reference (set via `set_condition_registry`, mirroring `ReferenceController`); the `handle_*` methods drop their per-call deps. The `finalizer.py` raw SQL is intentionally NOT touched — it lives inside `BEGIN IMMEDIATE` and must bypass auto-committing mixin methods.

**Tech Stack:** Python 3.11+, sqlite3, pytest, dataclasses.

---

## File Structure

**New code goes into existing mixin files:**
- `python/spinlab/db/segments.py` — `count_segments_for_run`, `count_segments_for_capture_session`, `has_competing_active_segment_for_endpoints`
- `python/spinlab/db/capture_runs.py` — `is_run_draft`
- `python/spinlab/db/attempts.py` — `attempt_exists`

**Migration touches:**
- `python/spinlab/capture/reference.py` — 6 callsites
- `python/spinlab/capture/recorder.py` — 2 callsites + constructor change
- `python/spinlab/capture/fill_gap.py` — 1 callsite (replaced with existing `get_segment_by_id`)
- `python/spinlab/state_builder.py` — 1 callsite
- `python/spinlab/routes/attempts.py` — 1 callsite

**NOT touched (intentional):**
- `python/spinlab/capture/finalizer.py` — inside `BEGIN IMMEDIATE`; mixin methods auto-commit and would break atomicity. The module docstring already documents this.
- Test files using `db.conn.execute(...)` for assertions — standard pytest pattern, out of scope.

---

## Conventions

- TDD: write the failing test first, see it fail, write the minimum code to pass, commit.
- One mixin method = one task = one commit.
- Migration tasks may batch all callsites within a single file into one commit (they're mechanical and share a rationale).
- Run `python -m pytest` (full suite) at the end of each phase.

---

## Phase 1: Add new Database methods (with TDD)

### Task 1: `count_segments_for_run` on SegmentsMixin

**Files:**
- Modify: `python/spinlab/db/segments.py`
- Test: `tests/unit/db/test_db_segments.py`

This single method covers six callsites; some need the `active = 1` filter, others don't. The signature accepts `active_only: bool = False` to encode that distinction.

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/db/test_db_segments.py`:

```python
def test_count_segments_for_run(tmp_path):
    from spinlab.db import Database
    from spinlab.models import Segment

    db = Database(tmp_path / "t.db")
    db.upsert_game("g", "G", "any%")
    db.create_capture_run("ref1", "g", "Run 1")

    def _seg(seg_id: str, active: bool = True):
        db.upsert_segment(Segment(
            id=seg_id, game_id="g", level_number=1,
            start_type="entrance", start_ordinal=0,
            end_type="goal", end_ordinal=0,
            reference_id="ref1", active=active,
        ))

    _seg("s1", active=True)
    _seg("s2", active=True)
    _seg("s3", active=False)

    assert db.count_segments_for_run("ref1") == 3
    assert db.count_segments_for_run("ref1", active_only=True) == 2
    assert db.count_segments_for_run("missing") == 0
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/unit/db/test_db_segments.py::test_count_segments_for_run -v
```

Expected: `AttributeError: 'Database' object has no attribute 'count_segments_for_run'`

- [ ] **Step 3: Add the method to SegmentsMixin**

In `python/spinlab/db/segments.py`, add to the `SegmentsMixin` class:

```python
    def count_segments_for_run(self, run_id: str, *, active_only: bool = False) -> int:
        """Count segments whose ``reference_id`` matches ``run_id``.

        ``active_only=True`` filters to ``active = 1`` (excludes soft-deleted).
        """
        if active_only:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM segments WHERE reference_id = ? AND active = 1",
                (run_id,),
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM segments WHERE reference_id = ?",
                (run_id,),
            ).fetchone()
        return int(row[0])
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m pytest tests/unit/db/test_db_segments.py::test_count_segments_for_run -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/db/segments.py tests/unit/db/test_db_segments.py
git commit -m "db: add count_segments_for_run helper"
```

---

### Task 2: `count_segments_for_capture_session` on SegmentsMixin

**Files:**
- Modify: `python/spinlab/db/segments.py`
- Test: `tests/unit/db/test_db_segments.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/db/test_db_segments.py`:

```python
def test_count_segments_for_capture_session(tmp_path):
    from spinlab.db import Database
    from spinlab.models import Segment

    db = Database(tmp_path / "t.db")
    db.upsert_game("g", "G", "any%")
    db.create_capture_run("ref1", "g", "Run 1")
    db.create_capture_session(session_id="sess1", capture_run_id="ref1", ordinal=1)
    db.create_capture_session(session_id="sess2", capture_run_id="ref1", ordinal=2)

    def _seg(seg_id: str, sess_id: str | None):
        db.upsert_segment(Segment(
            id=seg_id, game_id="g", level_number=1,
            start_type="entrance", start_ordinal=0,
            end_type="goal", end_ordinal=0,
            reference_id="ref1", capture_session_id=sess_id,
        ))

    _seg("s1", "sess1")
    _seg("s2", "sess1")
    _seg("s3", "sess2")

    assert db.count_segments_for_capture_session("sess1") == 2
    assert db.count_segments_for_capture_session("sess2") == 1
    assert db.count_segments_for_capture_session("missing") == 0
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/unit/db/test_db_segments.py::test_count_segments_for_capture_session -v
```

Expected: `AttributeError`.

- [ ] **Step 3: Add the method to SegmentsMixin**

In `python/spinlab/db/segments.py`, add to the `SegmentsMixin` class:

```python
    def count_segments_for_capture_session(self, session_id: str) -> int:
        """Count segments belonging to a specific capture session."""
        row = self.conn.execute(
            "SELECT COUNT(*) FROM segments WHERE capture_session_id = ?",
            (session_id,),
        ).fetchone()
        return int(row[0])
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m pytest tests/unit/db/test_db_segments.py::test_count_segments_for_capture_session -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/db/segments.py tests/unit/db/test_db_segments.py
git commit -m "db: add count_segments_for_capture_session helper"
```

---

### Task 3: `is_run_draft` on CaptureRunsMixin

**Files:**
- Modify: `python/spinlab/db/capture_runs.py`
- Test: `tests/unit/db/test_db_references.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/db/test_db_references.py` (inside `TestCaptureRunCRUD` class is fine, or as a module-level test):

```python
class TestIsRunDraft:
    def test_draft_returns_true(self, db):
        db.create_capture_run("r1", "g", "Run 1", draft=True)
        assert db.is_run_draft("r1") is True

    def test_non_draft_returns_false(self, db):
        db.create_capture_run("r1", "g", "Run 1", draft=False)
        assert db.is_run_draft("r1") is False

    def test_missing_returns_false(self, db):
        assert db.is_run_draft("does_not_exist") is False
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/unit/db/test_db_references.py::TestIsRunDraft -v
```

Expected: `AttributeError`.

- [ ] **Step 3: Add the method to CaptureRunsMixin**

In `python/spinlab/db/capture_runs.py`, add to the `CaptureRunsMixin` class:

```python
    def is_run_draft(self, run_id: str) -> bool:
        """True if ``run_id`` exists and is in draft state. Missing runs return False."""
        row = self.conn.execute(
            "SELECT draft FROM capture_runs WHERE id = ?", (run_id,),
        ).fetchone()
        return bool(row and row[0] == 1)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m pytest tests/unit/db/test_db_references.py::TestIsRunDraft -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/db/capture_runs.py tests/unit/db/test_db_references.py
git commit -m "db: add is_run_draft helper"
```

---

### Task 4: `has_competing_active_segment` on SegmentsMixin

**Files:**
- Modify: `python/spinlab/db/segments.py`
- Test: `tests/unit/db/test_db_segments.py`

Replaces the inline query in `SegmentRecorder._compute_is_primary`. Returns `True` if another active segment exists with the same endpoints (excluding the segment being checked).

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/db/test_db_segments.py`:

```python
def test_has_competing_active_segment(tmp_path):
    from spinlab.db import Database
    from spinlab.models import Segment

    db = Database(tmp_path / "t.db")
    db.upsert_game("g", "G", "any%")

    def _seg(seg_id: str, active: bool = True):
        db.upsert_segment(Segment(
            id=seg_id, game_id="g", level_number=1,
            start_type="entrance", start_ordinal=0,
            end_type="goal", end_ordinal=0,
            active=active,
        ))

    _seg("existing", active=True)

    # Same endpoints, different id, existing is active → competing
    assert db.has_competing_active_segment(
        game_id="g", level=1,
        start_type="entrance", start_ordinal=0,
        end_type="goal", end_ordinal=0,
        exclude_segment_id="new_seg",
    ) is True

    # Different endpoints → no competition
    assert db.has_competing_active_segment(
        game_id="g", level=2,
        start_type="entrance", start_ordinal=0,
        end_type="goal", end_ordinal=0,
        exclude_segment_id="new_seg",
    ) is False

    # Excluding the only matching segment → no competition
    assert db.has_competing_active_segment(
        game_id="g", level=1,
        start_type="entrance", start_ordinal=0,
        end_type="goal", end_ordinal=0,
        exclude_segment_id="existing",
    ) is False

    # Existing is inactive → no competition
    _seg("inactive", active=False)
    db.deactivate_segment("existing")
    assert db.has_competing_active_segment(
        game_id="g", level=1,
        start_type="entrance", start_ordinal=0,
        end_type="goal", end_ordinal=0,
        exclude_segment_id="new_seg",
    ) is False
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/unit/db/test_db_segments.py::test_has_competing_active_segment -v
```

Expected: `AttributeError`.

- [ ] **Step 3: Add the method to SegmentsMixin**

In `python/spinlab/db/segments.py`, add to the `SegmentsMixin` class:

```python
    def has_competing_active_segment(
        self,
        *,
        game_id: str,
        level: int,
        start_type: str,
        start_ordinal: int,
        end_type: str,
        end_ordinal: int,
        exclude_segment_id: str,
    ) -> bool:
        """True if another *active* segment shares these endpoints.

        Used by capture to compute ``is_primary``: a segment is primary iff
        no other active segment occupies the same (game, level, endpoints)
        slot. ``exclude_segment_id`` is the id of the segment being evaluated;
        excluding it lets the caller pass either a hypothetical-new id or an
        existing id and get a meaningful answer.
        """
        row = self.conn.execute(
            """SELECT id FROM segments
               WHERE game_id = ? AND level_number = ?
                 AND start_type = ? AND start_ordinal = ?
                 AND end_type = ? AND end_ordinal = ?
                 AND active = 1 AND id != ?""",
            (game_id, level, start_type, start_ordinal,
             end_type, end_ordinal, exclude_segment_id),
        ).fetchone()
        return row is not None
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m pytest tests/unit/db/test_db_segments.py::test_has_competing_active_segment -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/db/segments.py tests/unit/db/test_db_segments.py
git commit -m "db: add has_competing_active_segment helper"
```

---

### Task 5: `attempt_exists` on AttemptsMixin

**Files:**
- Modify: `python/spinlab/db/attempts.py`
- Test: `tests/unit/db/test_db_attempts.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/db/test_db_attempts.py`:

```python
def test_attempt_exists(tmp_path):
    from datetime import UTC, datetime
    from spinlab.db import Database
    from spinlab.models import Attempt, AttemptSource, Segment

    db = Database(tmp_path / "t.db")
    db.upsert_game("g", "G", "any%")
    db.upsert_segment(Segment(
        id="s1", game_id="g", level_number=1,
        start_type="entrance", start_ordinal=0,
        end_type="goal", end_ordinal=0,
    ))
    aid = db.log_attempt(Attempt(
        segment_id="s1", parent_id="sess1", completed=True,
        time_ms=1000, source=AttemptSource.PRACTICE,
        created_at=datetime.now(UTC),
    ))

    assert db.attempt_exists(aid) is True
    assert db.attempt_exists(999999) is False
```

Note: `log_attempt` returns the new row id; verify by reading current behavior or adjust the test if it returns None. If it returns None, fetch the id manually:

```python
    aid_row = db.conn.execute("SELECT id FROM attempts ORDER BY id DESC LIMIT 1").fetchone()
    aid = aid_row[0]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/unit/db/test_db_attempts.py::test_attempt_exists -v
```

Expected: `AttributeError`.

- [ ] **Step 3: Add the method to AttemptsMixin**

In `python/spinlab/db/attempts.py`, add to the `AttemptsMixin` class:

```python
    def attempt_exists(self, attempt_id: int) -> bool:
        """True if an attempt with this id exists."""
        row = self.conn.execute(
            "SELECT 1 FROM attempts WHERE id = ?", (attempt_id,),
        ).fetchone()
        return row is not None
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m pytest tests/unit/db/test_db_attempts.py::test_attempt_exists -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/db/attempts.py tests/unit/db/test_db_attempts.py
git commit -m "db: add attempt_exists helper"
```

---

### Task 6: Phase 1 verification

- [ ] **Step 1: Run the full fast suite**

```bash
python -m pytest -m "not emulator"
```

Expected: PASS (~790 tests).

If anything red, fix before proceeding.

---

## Phase 2: Migrate callers to new methods

Each task migrates all callsites in one file to keep commit messages tight and review focused.

### Task 7: Migrate `state_builder.py`

**Files:**
- Modify: `python/spinlab/state_builder.py:42-48`

Smallest, leaf-most callsite — start here to prove the pattern.

- [ ] **Step 1: Replace the raw SQL**

In `python/spinlab/state_builder.py`, replace lines 42-48:

```python
        sections_captured: int | None = None
        if is_recording:
            row = self.db.conn.execute(
                "SELECT COUNT(*) FROM segments WHERE reference_id = ? AND active = 1",
                (active_run_id,),
            ).fetchone()
            sections_captured = row[0]
```

with:

```python
        sections_captured: int | None = None
        if is_recording and active_run_id is not None:
            sections_captured = self.db.count_segments_for_run(
                active_run_id, active_only=True,
            )
```

(The `active_run_id is not None` guard makes the types line up cleanly; `count_segments_for_run` expects `str`, not `str | None`. `is_recording` already implies `active_run_id` is set, so this is defensive but free.)

- [ ] **Step 2: Run state_builder tests**

```bash
python -m pytest tests/unit/test_state_builder.py -v
```

Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add python/spinlab/state_builder.py
git commit -m "state_builder: use count_segments_for_run helper"
```

---

### Task 8: Migrate `routes/attempts.py`

**Files:**
- Modify: `python/spinlab/routes/attempts.py:25-29`

- [ ] **Step 1: Replace the raw SQL**

In `python/spinlab/routes/attempts.py`, replace lines 25-29:

```python
    row = db.conn.execute(
        "SELECT id FROM attempts WHERE id = ?", (attempt_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="attempt not found")
```

with:

```python
    if not db.attempt_exists(attempt_id):
        raise HTTPException(status_code=404, detail="attempt not found")
```

- [ ] **Step 2: Run attempts route tests**

```bash
python -m pytest tests/unit/test_attempts_invalidation.py tests/unit/test_invalidate_flow.py -v
```

Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add python/spinlab/routes/attempts.py
git commit -m "routes/attempts: use attempt_exists helper"
```

---

### Task 9: Migrate `capture/fill_gap.py`

**Files:**
- Modify: `python/spinlab/capture/fill_gap.py:44-49`

This callsite already has a perfectly good helper available: `get_segment_by_id` returns a `Segment` with `start_waypoint_id`.

- [ ] **Step 1: Replace the raw SQL**

In `python/spinlab/capture/fill_gap.py`, replace lines 44-49:

```python
        row = self._db.conn.execute(
            "SELECT start_waypoint_id FROM segments WHERE id = ?", (segment_id,),
        ).fetchone()
        start_waypoint_id = row[0] if row else None
        hot = (self._db.get_save_state(start_waypoint_id, "hot")
               if start_waypoint_id else None)
```

with:

```python
        seg = self._db.get_segment_by_id(segment_id)
        start_waypoint_id = seg.start_waypoint_id if seg else None
        hot = (self._db.get_save_state(start_waypoint_id, "hot")
               if start_waypoint_id else None)
```

- [ ] **Step 2: Run fill_gap tests**

```bash
python -m pytest tests/unit/capture/test_fill_gap.py -v
```

Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add python/spinlab/capture/fill_gap.py
git commit -m "fill_gap: use get_segment_by_id instead of raw SQL"
```

---

### Task 10: Migrate `capture/reference.py`

**Files:**
- Modify: `python/spinlab/capture/reference.py` — six callsites

The six raw SQL queries in `reference.py` fall into three categories: active-segment counts (lines 177, 524), all-segment counts (lines 300, 454), `is_run_draft` checks (lines 228, 396), and a per-session count (line 207).

- [ ] **Step 1: `get_paused_state` — line 177**

Replace:

```python
        seg_count = self.db.conn.execute(
            "SELECT COUNT(*) FROM segments WHERE reference_id = ? AND active = 1",
            (self.paused_run_id,),
        ).fetchone()[0]
```

with:

```python
        seg_count = self.db.count_segments_for_run(
            self.paused_run_id, active_only=True,
        )
```

- [ ] **Step 2: `_end_current_session` — lines 207 and 228**

Replace lines 206-211 (the segment-count-for-session query):

```python
            seg_count = self.db.conn.execute(
                "SELECT COUNT(*) FROM segments WHERE capture_session_id = ?",
                (sess_id,),
            ).fetchone()[0]
```

with:

```python
            seg_count = self.db.count_segments_for_capture_session(sess_id)
```

Replace lines 226-232 (the draft check that decides whether to pause or idle):

```python
        should_pause = False
        if run_id:
            row = self.db.conn.execute(
                "SELECT draft FROM capture_runs WHERE id = ?", (run_id,)
            ).fetchone()
            should_pause = bool(row and row[0] == 1)
```

with:

```python
        should_pause = bool(run_id and self.db.is_run_draft(run_id))
```

- [ ] **Step 3: `stop_reference` — line 300**

Replace lines 300-303:

```python
        seg_count_in_run = self.db.conn.execute(
            "SELECT COUNT(*) FROM segments WHERE reference_id = ?",
            (self.recorder.capture_run_id,),
        ).fetchone()[0] if self.recorder.capture_run_id else 0
```

with:

```python
        seg_count_in_run = (
            self.db.count_segments_for_run(self.recorder.capture_run_id)
            if self.recorder.capture_run_id else 0
        )
```

- [ ] **Step 4: `delete_capture_session` — line 396**

Replace lines 396-400:

```python
        row = self.db.conn.execute(
            "SELECT draft FROM capture_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if not row or row[0] != 1:
            raise SessionDeleteAfterFinalizeError()
```

with:

```python
        if not self.db.is_run_draft(run_id):
            raise SessionDeleteAfterFinalizeError()
```

- [ ] **Step 5: `stop_replay` — line 454**

Replace lines 453-457:

```python
        if run_id:
            seg_count = self.db.conn.execute(
                "SELECT COUNT(*) FROM segments WHERE reference_id = ?",
                (run_id,),
            ).fetchone()[0]
```

with:

```python
        if run_id:
            seg_count = self.db.count_segments_for_run(run_id)
```

- [ ] **Step 6: `handle_replay_error` — line 524**

Replace lines 523-527:

```python
        run_id = self.recorder.capture_run_id
        seg_count = self.db.conn.execute(
            "SELECT COUNT(*) FROM segments WHERE reference_id = ?",
            (run_id,),
        ).fetchone()[0] if run_id else 0
```

with:

```python
        run_id = self.recorder.capture_run_id
        seg_count = self.db.count_segments_for_run(run_id) if run_id else 0
```

- [ ] **Step 7: Verify no remaining `db.conn.execute` callsites in this file**

```bash
grep -n "db\.conn\.execute" python/spinlab/capture/reference.py
```

Expected: no output (file is clean).

- [ ] **Step 8: Run capture tests**

```bash
python -m pytest tests/unit/capture/ -v
```

Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add python/spinlab/capture/reference.py
git commit -m "capture/reference: replace raw SQL with Database helpers"
```

---

### Task 11: Migrate `capture/recorder.py` raw SQL

**Files:**
- Modify: `python/spinlab/capture/recorder.py` — two callsites

The recorder constructor changes happen in Phase 3 (Task 13); here we only swap the SQL.

- [ ] **Step 1: `_close_segment` — line 103**

Replace lines 103-106:

```python
        existing_count = db.conn.execute(
            "SELECT COUNT(*) FROM segments WHERE reference_id = ?",
            (self.capture_run_id,),
        ).fetchone()[0]
```

with:

```python
        existing_count = (
            db.count_segments_for_run(self.capture_run_id)
            if self.capture_run_id else 0
        )
```

- [ ] **Step 2: `_compute_is_primary` — line 152**

Replace the entire `_compute_is_primary` static method (lines 149-161):

```python
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
```

with:

```python
    @staticmethod
    def _compute_is_primary(db, game_id, level, start_type, start_ord,
                            end_type, end_ord, new_seg_id) -> bool:
        return not db.has_competing_active_segment(
            game_id=game_id, level=level,
            start_type=start_type, start_ordinal=start_ord,
            end_type=end_type, end_ordinal=end_ord,
            exclude_segment_id=new_seg_id,
        )
```

- [ ] **Step 3: Verify no remaining `db.conn.execute` callsites**

```bash
grep -n "db\.conn\.execute" python/spinlab/capture/recorder.py
```

Expected: no output.

- [ ] **Step 4: Run recorder + capture tests**

```bash
python -m pytest tests/unit/capture/ tests/unit/test_segments_is_primary.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/capture/recorder.py
git commit -m "capture/recorder: replace raw SQL with Database helpers"
```

---

### Task 12: Phase 2 verification

- [ ] **Step 1: Confirm only intentional raw SQL remains in production code**

```bash
grep -rn "db\.conn\.execute\|self\.db\.conn\.execute\|self\._db\.conn\.execute" python/spinlab/ --include="*.py"
```

Expected: only matches in `python/spinlab/capture/finalizer.py` (intentional, inside `BEGIN IMMEDIATE`) and inside `python/spinlab/db/` modules (where raw SQL belongs).

- [ ] **Step 2: Run the full fast suite**

```bash
python -m pytest -m "not emulator"
```

Expected: PASS.

---

## Phase 3: SegmentRecorder constructor injection (C5)

`SegmentRecorder` currently takes `db` and `registry` on every `handle_*` call — both are stable for the recorder's lifetime. Move them to `__init__`. Because the active condition registry is replaced on game-switch via `set_condition_registry` (mirroring `ReferenceController`), the recorder needs the same setter.

### Task 13: Update `SegmentRecorder` to take db + registry at construction

**Files:**
- Modify: `python/spinlab/capture/recorder.py`
- Modify: `python/spinlab/capture/reference.py`
- Modify: `tests/unit/capture/test_recorder.py` (if present — adjust constructor calls)

- [ ] **Step 1: Update `SegmentRecorder.__init__` and the `handle_*` signatures**

In `python/spinlab/capture/recorder.py`, replace the constructor (lines 47-54):

```python
    def __init__(self) -> None:
        self.capture_run_id: str | None = None
        self.current_capture_session_id: str | None = None
        self.pending_start: PendingStart | None = None
        self.died: bool = False
        self.rec_path: str | None = None
        self._deaths_in_segment: int = 0
        self._last_spawn_ms: int | None = None
```

with:

```python
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

    def set_condition_registry(self, registry: "ConditionRegistry") -> None:
        """Swap the active condition registry (called on game-switch)."""
        self._condition_registry = registry
```

Update `_close_segment` (currently lines 81-147) to drop the `db` and `registry` parameters and use `self._db` / `self._condition_registry`:

```python
    def _close_segment(self, game_id, start: PendingStart, end_type, end_ordinal,
                       level, end_raw_conditions,
                       end_timestamp_ms: int | None = None) -> None:
        """Create waypoints + segment for the segment ending here, persist timing."""
        from ..models import Segment, Waypoint, WaypointSaveState

        start_conds = self._condition_registry.decode(start.raw_conditions, level=level)
        end_conds = self._condition_registry.decode(end_raw_conditions, level=level)

        wp_start = Waypoint.make(game_id, level, start.type,
                                 start.ordinal, start_conds)
        wp_end = Waypoint.make(game_id, level, end_type, end_ordinal, end_conds)
        self._db.upsert_waypoint(wp_start)
        self._db.upsert_waypoint(wp_end)

        seg_id = Segment.make_id(
            game_id, level, start.type, start.ordinal,
            end_type, end_ordinal, wp_start.id, wp_end.id,
        )
        is_primary = self._compute_is_primary(
            self._db, game_id, level, start.type, start.ordinal,
            end_type, end_ordinal, seg_id)
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
            reference_id=self.capture_run_id,
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
                is_default=True,
            ))

        # Persist timing immediately so a crash before finalize keeps the data.
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
            self._db.add_recorded_segment_time(
                self.current_capture_session_id, seg_id,
                time_ms=time_ms, deaths=deaths, clean_tail_ms=clean_tail_ms,
            )

        self._deaths_in_segment = 0
        self._last_spawn_ms = None
```

Update the three `handle_*` methods to drop their `db` + `registry` parameters (lines 163-220):

```python
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

    def handle_exit(self, event: LevelExitEvent, game_id: str) -> None:
        if event.goal == "abort":
            self.pending_start = None
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
            state_path=cold_path, is_default=True))
        logger.debug("Stored cold save state for waypoint %s: %s", wp.id, cold_path)
```

- [ ] **Step 2: Update `ReferenceController` to pass deps to SegmentRecorder**

In `python/spinlab/capture/reference.py`, update the `ReferenceController.__init__` (lines 96-107):

Replace:

```python
    def __init__(self, db: "Database", emu: "EmuBackend") -> None:
        self.db = db
        self.emu = emu
        self.recorder = SegmentRecorder()
        self.condition_registry: ConditionRegistry = ConditionRegistry()
```

with:

```python
    def __init__(self, db: "Database", emu: "EmuBackend") -> None:
        self.db = db
        self.emu = emu
        self.condition_registry: ConditionRegistry = ConditionRegistry()
        self.recorder = SegmentRecorder(db, self.condition_registry)
```

Update `set_condition_registry` (line 109) to propagate to the recorder:

Replace:

```python
    def set_condition_registry(self, registry: ConditionRegistry) -> None:
        self.condition_registry = registry
```

with:

```python
    def set_condition_registry(self, registry: ConditionRegistry) -> None:
        self.condition_registry = registry
        self.recorder.set_condition_registry(registry)
```

Update the three controller methods that currently forward `db` + `registry` (lines 484, 497, 508, 513) to drop them:

Replace `handle_checkpoint` (line 484-498):

```python
    async def handle_checkpoint(self, event: CheckpointEvent, game_id: str) -> None:
        logger.info("capture: checkpoint level=%s cp=%s",
                     event.level_num, event.cp_ordinal)
        if self.is_recording:
            from spinlab.state_paths import segment_id_for_event
            seg_id = segment_id_for_event(event)
            if seg_id:
                try:
                    await self.emu.save_state(seg_id)
                except Exception:
                    logger.exception(
                        "save_state failed for checkpoint event seg_id=%r", seg_id,
                    )
        self.recorder.handle_checkpoint(event, game_id, self.db,
                                           self.condition_registry)
```

with:

```python
    async def handle_checkpoint(self, event: CheckpointEvent, game_id: str) -> None:
        logger.info("capture: checkpoint level=%s cp=%s",
                     event.level_num, event.cp_ordinal)
        if self.is_recording:
            from spinlab.state_paths import segment_id_for_event
            seg_id = segment_id_for_event(event)
            if seg_id:
                try:
                    await self.emu.save_state(seg_id)
                except Exception:
                    logger.exception(
                        "save_state failed for checkpoint event seg_id=%r", seg_id,
                    )
        self.recorder.handle_checkpoint(event, game_id)
```

Replace `handle_spawn` (lines 504-509):

```python
    def handle_spawn(self, event: SpawnEvent, game_id: str) -> None:
        logger.info("capture: spawn level=%s state_path=%s",
                     event.level_num, event.state_path)
        self.recorder.handle_spawn_timing(timestamp_ms=event.timestamp_ms)
        self.recorder.handle_spawn(event, game_id, self.db,
                                      self.condition_registry)
```

with:

```python
    def handle_spawn(self, event: SpawnEvent, game_id: str) -> None:
        logger.info("capture: spawn level=%s state_path=%s",
                     event.level_num, event.state_path)
        self.recorder.handle_spawn_timing(timestamp_ms=event.timestamp_ms)
        self.recorder.handle_spawn(event, game_id)
```

Replace `handle_exit` (lines 511-514):

```python
    def handle_exit(self, event: LevelExitEvent, game_id: str) -> None:
        logger.info("capture: exit level=%s", event.level)
        self.recorder.handle_exit(event, game_id, self.db,
                                     self.condition_registry)
```

with:

```python
    def handle_exit(self, event: LevelExitEvent, game_id: str) -> None:
        logger.info("capture: exit level=%s", event.level)
        self.recorder.handle_exit(event, game_id)
```

- [ ] **Step 3: Update tests that construct SegmentRecorder directly**

```bash
grep -rn "SegmentRecorder()" tests/ python/
```

For each match, update to `SegmentRecorder(db, ConditionRegistry())` or whatever the test's existing fixtures provide. (Most tests will go through `ReferenceController`, which handles this internally — so the blast radius is likely small.)

Likewise update any `recorder.handle_checkpoint(..., db, registry)` or `recorder.handle_spawn(..., db, registry)` callsites in tests:

```bash
grep -rn "recorder\.handle_\(checkpoint\|spawn\|exit\)" tests/ python/
```

Drop the trailing `db`, `registry` arguments at each callsite.

- [ ] **Step 4: Run the capture test suite**

```bash
python -m pytest tests/unit/capture/ -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/capture/recorder.py python/spinlab/capture/reference.py tests/
git commit -m "capture: inject db + condition_registry into SegmentRecorder constructor"
```

---

## Phase 4: Final verification

### Task 14: Full suite + type check

- [ ] **Step 1: Run the full pytest suite (including emulator + frontend)**

```bash
python -m pytest
```

Expected: PASS. Per `CLAUDE.md`, this is the bar before declaring Bundle 1 done.

If emulator tests fail and you don't have RA running, document that as a precondition gap and report which subsets you did run.

- [ ] **Step 2: Type-check the changed modules**

```bash
npx pyright python/spinlab/db/segments.py python/spinlab/db/capture_runs.py python/spinlab/db/attempts.py python/spinlab/capture/recorder.py python/spinlab/capture/reference.py python/spinlab/capture/fill_gap.py python/spinlab/state_builder.py python/spinlab/routes/attempts.py
```

Expected: no new errors. Pre-existing errors are out of scope.

- [ ] **Step 3: Audit remaining raw SQL outside `db/`**

```bash
grep -rn "\.conn\.execute" python/spinlab/ --include="*.py" | grep -v "^python/spinlab/db/"
```

Expected: only matches in `python/spinlab/capture/finalizer.py` (inside `BEGIN IMMEDIATE`, intentional).

- [ ] **Step 4: Update memory**

Add a brief project memory note that Bundle 1 of the encapsulation pass is complete and what changed at a high level — useful for future cleanup work.

---

## Out of Scope (Bundle 2 / Bundle 3)

- RAClient extraction (Bundle 2 — separate plan)
- Poller detector injection (Bundle 2 — separate plan)
- Replay flow rework: `replay_total` relocation + mode-flip race (Bundle 3 — separate plan)
- `finalizer.py` raw SQL — intentionally inside `BEGIN IMMEDIATE`
- Tests reading `db.conn.execute` for assertion-time DB queries — standard pytest pattern

---

## Self-Review

**Spec coverage:**
- A1 (raw SQL → Database methods): Tasks 1-12 cover every production-code callsite identified in the audit. The finalizer exclusion is explicit.
- C5 (SegmentRecorder constructor injection): Task 13.

**Placeholder scan:** No TBDs, no "implement appropriate handling," no "similar to Task N." Each step has its replacement code inline.

**Type consistency:**
- `count_segments_for_run(run_id: str, *, active_only: bool = False) -> int` — used consistently in Tasks 7, 10, 11.
- `count_segments_for_capture_session(session_id: str) -> int` — used in Task 10.
- `is_run_draft(run_id: str) -> bool` — used in Task 10.
- `has_competing_active_segment(*, game_id, level, start_type, start_ordinal, end_type, end_ordinal, exclude_segment_id) -> bool` — used in Task 11. Note: kwargs-only to keep callsites readable.
- `attempt_exists(attempt_id: int) -> bool` — used in Task 8.

Names match between the definitions in Phase 1 and the migrations in Phase 2.
