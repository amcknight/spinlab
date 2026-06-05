# Run↔Segment Membership (Design X) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Manage tab's run-scoped segment views report the segments a run *traversed* (already recorded in `attempts`) instead of which run *owns* the row, so a reference run that re-records existing levels shows its captured segments.

**Architecture:** Two DB-layer query rewrites. `get_segments_by_reference` and the `run_id`-scoped branch of `segments_missing_cold` switch from `WHERE s.capture_run_id = ?` (ownership, first-writer-wins) to a traversal-membership subquery over `attempts` (`segment_id` for that `capture_run_id`, non-invalidated) — mirroring the existing `count_segments_traversed_in_run`. No schema change, no migration, no recorder/replay/model change.

**Tech Stack:** Python 3.11+, sqlite3, pytest.

Spec: `docs/superpowers/specs/2026-06-04-run-segment-membership-design.md`.

---

## File structure

- Modify: `python/spinlab/db/capture_runs.py` — `get_segments_by_reference` query.
- Modify: `python/spinlab/db/segments.py` — `segments_missing_cold` run-scoped branch.
- Test: `tests/unit/db/test_db_references.py` — update `test_get_segments_by_reference`; add a cross-ownership test.
- Test: `tests/unit/db/test_db_segments.py` — update `test_segments_missing_cold_scoped_by_run`; add a cross-ownership test.

---

### Task 1: `get_segments_by_reference` → traversal membership

**Files:**
- Modify: `python/spinlab/db/capture_runs.py:166-191` (`get_segments_by_reference`)
- Test: `tests/unit/db/test_db_references.py`

- [ ] **Step 1: Add imports to the test module**

In `tests/unit/db/test_db_references.py`, change the models import (line 5):

```python
from spinlab.models import AttemptOutcome, AttemptSource, EventAttempt, Segment
```

- [ ] **Step 2: Make the existing test traversal-compatible (stays green under current code)**

Replace `test_get_segments_by_reference` (currently `tests/unit/db/test_db_references.py:131-137`) with — it now also records an event row per segment for the run, which the ownership query ignores (still green) but the new traversal query will require:

```python
    def test_get_segments_by_reference(self, db):
        db.create_capture_run("ref1", "g", "Run 1")
        segs = [_make_segment(db, "g", i, ordinal=i + 1, ref_id="ref1") for i in range(3)]
        for s in segs:
            db.log_event_attempt(EventAttempt(
                segment_id=s.id, episode_id=f"ep_{s.id}",
                outcome=AttemptOutcome.SURVIVED, time_ms=1000,
                capture_run_id="ref1", source=AttemptSource.REFERENCE,
            ))
        rows = db.get_segments_by_reference("ref1")
        assert len(rows) == 3
        assert rows[0]["ordinal"] == 1
```

- [ ] **Step 3: Run it — confirm still green under current (ownership) code**

Run: `python -m pytest tests/unit/db/test_db_references.py::TestSegmentEdit::test_get_segments_by_reference -v`
Expected: PASS (ownership query returns the 3 owned segments; the added events are ignored).

- [ ] **Step 4: Write the failing cross-ownership test**

Add this method to `class TestSegmentEdit` in `tests/unit/db/test_db_references.py`, right after `test_get_segments_by_reference`:

```python
    def test_get_segments_by_reference_includes_traversed_not_just_owned(self, db):
        """A re-recording run shows segments it traversed even though an
        earlier run still *owns* the row (capture_run_id)."""
        db.create_capture_run("old", "g", "Old Run")
        db.create_capture_run("new", "g", "New Run")
        seg = _make_segment(db, "g", 1, ordinal=1, ref_id="old")
        db.log_event_attempt(EventAttempt(
            segment_id=seg.id, episode_id="ep1",
            outcome=AttemptOutcome.SURVIVED, time_ms=1000,
            capture_run_id="new", source=AttemptSource.REFERENCE,
        ))
        rows = db.get_segments_by_reference("new")
        assert [r["id"] for r in rows] == [seg.id]
```

- [ ] **Step 5: Run it — confirm it FAILS**

Run: `python -m pytest "tests/unit/db/test_db_references.py::TestSegmentEdit::test_get_segments_by_reference_includes_traversed_not_just_owned" -v`
Expected: FAIL — `assert [] == ['g:1:entrance.0:goal.0:stub_sta:stub_end']` (ownership query returns nothing for run "new").

- [ ] **Step 6: Implement traversal membership**

In `python/spinlab/db/capture_runs.py`, in `get_segments_by_reference`, change only the WHERE clause:

```python
               WHERE s.active = 1
                 AND s.id IN (
                   SELECT DISTINCT a.segment_id FROM attempts a
                   WHERE a.capture_run_id = ? AND a.invalidated = 0
                 )
               ORDER BY s.ordinal""",
            (capture_run_id,),
```

(The SELECT list, the `LEFT JOIN capture_sessions cs`, and the param binding to a single `capture_run_id` are unchanged — `session_ordinal` still comes from `s.capture_session_id`; per the spec, for re-records that is the owner's session, an accepted display caveat.)

- [ ] **Step 7: Run both tests — confirm GREEN**

Run: `python -m pytest tests/unit/db/test_db_references.py -v`
Expected: PASS (both the updated `test_get_segments_by_reference` and the new cross-ownership test, plus the rest of the file).

- [ ] **Step 8: Commit**

```bash
git add python/spinlab/db/capture_runs.py tests/unit/db/test_db_references.py
git commit -m "fix(db): get_segments_by_reference scopes by traversal not ownership

Manage's run-scoped segment view used segments.capture_run_id (first-writer-
wins ownership), so a run that re-recorded existing levels showed zero
segments. Switch to traversal membership derived from attempts (mirrors
count_segments_traversed_in_run): a run lists segments it recorded a
non-invalidated event for. Spec: 2026-06-04-run-segment-membership-design.md.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: run-scoped `segments_missing_cold` → traversal membership

**Files:**
- Modify: `python/spinlab/db/segments.py:153-181` (`segments_missing_cold`)
- Test: `tests/unit/db/test_db_segments.py`

- [ ] **Step 1: Add imports to the test module**

In `tests/unit/db/test_db_segments.py`, change the models import (line 3):

```python
from spinlab.models import AttemptOutcome, AttemptSource, EndpointType, EventAttempt, Segment, Waypoint
```

- [ ] **Step 2: Make the existing run-scoped test traversal-compatible (stays green under current code)**

In `test_segments_missing_cold_scoped_by_run` (`tests/unit/db/test_db_segments.py:110-143`), the `mk` helper currently creates a waypoint + segment + hot save state owned by `run_id`. After the `mk("segA", "rA", 1)` / `mk("segB", "rB", 2)` calls (line 136), add an event row per segment so each run *traverses* the segment it owns (ignored by the current ownership query, required by the new one):

```python
    mk("segA", "rA", 1)
    mk("segB", "rB", 2)
    db.log_event_attempt(EventAttempt(
        segment_id="segA", episode_id="epA",
        outcome=AttemptOutcome.SURVIVED, time_ms=1000,
        capture_run_id="rA", source=AttemptSource.REFERENCE,
    ))
    db.log_event_attempt(EventAttempt(
        segment_id="segB", episode_id="epB",
        outcome=AttemptOutcome.SURVIVED, time_ms=1000,
        capture_run_id="rB", source=AttemptSource.REFERENCE,
    ))
```

- [ ] **Step 3: Run it — confirm still green under current (ownership) code**

Run: `python -m pytest tests/unit/db/test_db_segments.py::test_segments_missing_cold_scoped_by_run -v`
Expected: PASS (ownership scoping still returns `{"segA"}` for `run_id="rA"`; events ignored).

- [ ] **Step 4: Write the failing cross-ownership test**

Add to `tests/unit/db/test_db_segments.py` (module-level, after `test_segments_missing_cold_scoped_by_run`):

```python
def test_segments_missing_cold_scoped_by_traversal_not_ownership(tmp_path):
    """Cold-fill for a run must include segments the run traversed, even when
    an earlier run owns the row."""
    db = Database(tmp_path / "t.db")
    db.upsert_game("g", "G", "any%")
    db.create_capture_run("old", "g", "Old", kind="live")
    db.promote_draft("old", "Old")
    db.create_capture_run("new", "g", "New", kind="live")

    wp = Waypoint.make("g", 1, "checkpoint", 1, {})
    db.upsert_waypoint(wp)
    db.upsert_segment(Segment(
        id="segX", game_id="g", level_number=1,
        start_type="checkpoint", start_ordinal=1,
        end_type="goal", end_ordinal=0,
        start_waypoint_id=wp.id, end_waypoint_id=wp.id,
        capture_run_id="old",
    ))
    db.add_save_state(WaypointSaveState(wp.id, "hot", "/segX.state"))
    # 'new' traverses segX (no cold state exists yet).
    db.log_event_attempt(EventAttempt(
        segment_id="segX", episode_id="epX",
        outcome=AttemptOutcome.SURVIVED, time_ms=1000,
        capture_run_id="new", source=AttemptSource.REFERENCE,
    ))

    scoped = {g["segment_id"] for g in db.segments_missing_cold("g", run_id="new")}
    assert scoped == {"segX"}
    db.close()
```

Note: `WaypointSaveState` is already imported in this file (used by the existing `mk` helper). If not, add it to the models import.

- [ ] **Step 5: Run it — confirm it FAILS**

Run: `python -m pytest tests/unit/db/test_db_segments.py::test_segments_missing_cold_scoped_by_traversal_not_ownership -v`
Expected: FAIL — `assert set() == {'segX'}` (ownership scoping excludes segX because run "new" owns nothing).

- [ ] **Step 6: Implement traversal membership**

In `python/spinlab/db/segments.py`, in `segments_missing_cold`, change the run-scoped branch (currently lines 161-164):

```python
        run_clause = ""
        if run_id is not None:
            run_clause = ("AND s.id IN (SELECT DISTINCT segment_id FROM attempts "
                          "WHERE capture_run_id = ? AND invalidated = 0)")
            params.append(run_id)
```

(The `run_id=None` whole-game branch and the rest of the query are unchanged.)

- [ ] **Step 7: Run the file — confirm GREEN**

Run: `python -m pytest tests/unit/db/test_db_segments.py -v`
Expected: PASS (updated scoped test + new traversal test + rest of file).

- [ ] **Step 8: Commit**

```bash
git add python/spinlab/db/segments.py tests/unit/db/test_db_segments.py
git commit -m "fix(db): segments_missing_cold run-scope uses traversal not ownership

Run-scoped cold-fill used segments.capture_run_id, so a re-recording run's
traversed segments were excluded. Scope by traversal membership from attempts,
matching get_segments_by_reference.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Full-suite gate

- [ ] **Step 1: Run the full suite**

Run: `python -m pytest`
Expected: all pass, 0 skipped (per project policy; emulator + frontend smoke included). The known segments-v07 `RuntimeWarning` in `api.py:165` is the only allowed warning.

- [ ] **Step 2: If anything unrelated is red**

Stop and report — do not paper over. Per project policy a red baseline blocks the merge.

- [ ] **Step 3: (Manual, optional) live confirm**

On a dashboard with a game that has an existing reference run, do a second reference run over the same levels, save it, open Manage → its Segments section now lists the captured segments (previously empty).

---

## Self-Review

**Spec coverage:** Both query rewrites (Goals 1 + 2) → Tasks 1 + 2. Goal 3 (no schema/migration/recorder change) → satisfied by construction. The "model stuck" item needs no code per spec (already geography-pooled). Covered.

**Placeholder scan:** None — every step has exact code/commands.

**Type/name consistency:** `EventAttempt`, `AttemptOutcome.SURVIVED`, `AttemptSource.REFERENCE`, `WaypointSaveState`, `Waypoint.make`, `db.log_event_attempt`, `db.add_save_state`, `db.get_segments_by_reference`, `db.segments_missing_cold(game_id, run_id=)` all match current signatures (verified against `python/spinlab/models.py`, `db/attempts.py`, `db/segments.py`, `db/capture_runs.py`, and existing tests).

**Edge case:** traversal membership requires ≥1 non-invalidated event row per captured segment — the same assumption `count_segments_traversed_in_run` already relies on and the live counter already trusts.
