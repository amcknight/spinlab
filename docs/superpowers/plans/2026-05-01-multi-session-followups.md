# Multi-Session Follow-ups Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the cohesive correctness/dashboard/observability/hygiene cluster from the multi-session follow-ups spec — six independently mergeable sections that finish making the multi-session reference run feature usable, observable, and clean.

**Architecture:** Each spec section maps to one or more tasks. Tasks within a section share a commit; sections do not. Tests-first throughout; the existing `pytest` and Vitest suites must stay green between every commit.

**Tech Stack:** Python 3.11 + pytest, TypeScript + Vitest, SQLite (drop-on-mismatch greenfield schema), FastAPI, Lua.

**Reference docs:**
- Spec: [docs/superpowers/specs/2026-05-01-multi-session-followups-design.md](../specs/2026-05-01-multi-session-followups-design.md)
- Backlog: [multi-session-followups.txt](../../../multi-session-followups.txt)
- Multi-session base spec: [docs/superpowers/specs/2026-05-01-multi-session-reference-runs-design.md](../specs/2026-05-01-multi-session-reference-runs-design.md)

**Pre-flight:**

Before starting any task, run the full suite to establish a baseline:

```
python -m pytest
cd frontend && npm test && cd ..
```

Both must pass green. If they don't, fix or document the pre-existing failures before starting Task 1.

---

## File Map

Files touched by this plan, in roughly the order they appear:

| File | Sections | Why |
|------|----------|-----|
| `python/spinlab/protocol.py` | §1 | Add `timestamp_ms` field to `SpawnEvent`. |
| `python/spinlab/capture/reference.py` | §1, §2, §4, §6.2, §6.3, §6.5 | Spawn-timing call site, scheduler rebuild, session-end log, exception swap, delete-session guard. |
| `python/spinlab/capture/recorder.py` | (read-only reference) | `handle_spawn_timing` already accepts `int`. |
| `python/spinlab/db/capture_sessions.py` | §3.1, §4.1, §4.4 | Segment count subquery, recovery warnings, recovery summary. |
| `python/spinlab/db/capture_runs.py` | §3.2, §4.3 | LEFT JOIN for session_ordinal; unlink-failure logging. |
| `python/spinlab/db/core.py` | §6.1, §6.4 | Schema rename + partial unique index. |
| `python/spinlab/db/attempts.py` | §6.1 | Column rename in queries. |
| `python/spinlab/models.py` | §6.1 | `Attempt.session_id` → `Attempt.parent_id`. |
| `python/spinlab/practice.py`, `python/spinlab/speed_run.py` | §6.1 | `Attempt(...)` construction sites. |
| `python/spinlab/errors.py` | §6.2, §6.3, §6.5 | New `NoPausedRunError`, `SessionInUseError`; drop `RunPendingError` alias. |
| `python/spinlab/state_builder.py` | (no change) | Already emits `int | None`; type annotation lives in TS. |
| `frontend/src/types.ts` | §3.1, §3.2, §3.3 | `CaptureSession.segment_count`, `ReferenceSegment.session_ordinal`, `sections_captured: number \| null`. |
| `frontend/src/manage.ts` | §3.1, §3.2, §3.5 | Render segment_count + session column; empty-state copy. |
| `frontend/src/manage.test.ts` (new) | §3.4 | Click-handler tests for resume/save-and-finish/discard-run. |
| `tests/unit/capture/test_recorder.py` | §1 | New unit test for `clean_tail_ms < time_ms` with deaths. |
| `tests/unit/capture/test_multi_session.py` | §2, §5, §6.2, §6.5 | New tests for rebuild-on-zero-seed, replay-paused recovery, NoPausedRunError, delete-active-session guard. |
| `tests/integration/test_crash_recovery.py` | §5 | New integration test for paused-A → replay-B → restart. |

---

## Task 1: Fix `clean_tail_ms` for segments with deaths (§1)

**Why:** `SpawnEvent` doesn't carry `timestamp_ms` to Python today, so `_last_spawn_ms` is never set, so `clean_tail_ms` falls back to `time_ms` for every segment with a mid-segment death. Lua already emits the field — `lua/spinlab.lua:583` sends `timestamp_ms = ts_ms()` — but `parse_event` in `protocol.py:257` strips it because `SpawnEvent` doesn't declare it.

**Files:**
- Modify: `python/spinlab/protocol.py:54-62`
- Modify: `python/spinlab/capture/reference.py:514-519`
- Test: `tests/unit/capture/test_recorder.py` (add new test)

- [ ] **Step 1.1: Read existing fixtures.**

Open `tests/unit/capture/test_recorder.py` and `tests/unit/capture/conftest.py` (if present) to understand the existing `db` and `registry` fixtures, and how `SegmentRecorder` is constructed in current tests. Match those patterns in the new test below.

- [ ] **Step 1.2: Write the failing test (SpawnEvent path).**

Append to `tests/unit/capture/test_recorder.py`. The test exercises the *call path* from `ReferenceController.handle_spawn` → `recorder.handle_spawn_timing`, which is where the bug actually lives. (The recorder's own logic is fine; calling `handle_spawn_timing(3000)` directly works. The bug is that `ReferenceController.handle_spawn` was hard-coding `None`.)

```python
def test_handle_spawn_event_propagates_timestamp_ms(db, registry):
    """ReferenceController.handle_spawn must pass event.timestamp_ms through to
    the recorder's _last_spawn_ms, otherwise clean_tail_ms is always == time_ms
    for any segment with deaths. Regression test for the multi-session work."""
    from spinlab.capture.reference import ReferenceController
    from spinlab.protocol import (
        LevelEntranceEvent, LevelExitEvent, SpawnEvent, DeathEvent,
    )

    from tests.conftest import FakeTcpManager
    # The module-level `db` fixture already pre-creates game="g1", run="run1",
    # session="sess1" — reuse those rather than building a parallel fixture.
    ctl = ReferenceController(db, FakeTcpManager(connected=False))
    ctl.recorder.capture_run_id = "run1"
    ctl.recorder.current_capture_session_id = "sess1"

    ctl.handle_entrance(LevelEntranceEvent(
        level=1, state_path=None, timestamp_ms=1000, conditions={},
    ))
    ctl.handle_death(DeathEvent())
    ctl.handle_spawn(SpawnEvent(
        level_num=1, state_captured=False, state_path=None,
        is_cold_cp=False, cp_ordinal=None,
        timestamp_ms=3000, conditions={},
    ), game_id="g1")
    ctl.handle_exit(LevelExitEvent(
        level=1, goal="exit", timestamp_ms=5000, conditions={},
    ), game_id="g1")

    rows = db.drain_recorded_segment_times_for_run("run1")
    assert len(rows) == 1
    row = rows[0]
    assert row["deaths"] == 1
    assert row["time_ms"] == 4000        # 5000 - 1000
    assert row["clean_tail_ms"] == 2000  # 5000 - 3000 (NOT 4000)
```

> If `ReferenceController` requires `data_dir` to be a real `Path` (not `None`), pass `tmp_path` from the pytest fixture instead. If `condition_registry` cannot be `None` either, use the same fixture other tests in the file use (likely `registry` via conftest). Adjust the constructor call to match the local pattern rather than fighting the type hints.

- [ ] **Step 1.3: Run the test to verify it fails.**

```
python -m pytest tests/unit/capture/test_recorder.py::test_handle_spawn_event_propagates_timestamp_ms -v
```

Expected: FAIL. Either the assertion `rows[0]["clean_tail_ms"] == 2000` fails with the actual value being `4000` (== `time_ms`), or the test fails at construction with `SpawnEvent` not accepting `timestamp_ms`.

- [ ] **Step 1.4: Add `timestamp_ms` to `SpawnEvent`.**

Edit `python/spinlab/protocol.py`:

```python
@dataclass
class SpawnEvent:
    event: str = "spawn"
    level_num: int = 0
    state_captured: bool = False
    state_path: str | None = None
    conditions: dict = field(default_factory=dict)
    is_cold_cp: bool = False
    cp_ordinal: int | None = None
    timestamp_ms: int = 0
```

- [ ] **Step 1.5: Wire the timestamp through `handle_spawn`.**

Edit `python/spinlab/capture/reference.py:514-519`:

```python
def handle_spawn(self, event: SpawnEvent, game_id: str) -> None:
    logger.info("capture: spawn level=%s state_captured=%s",
                 event.level_num, event.state_captured)
    self.recorder.handle_spawn_timing(timestamp_ms=event.timestamp_ms)
    self.recorder.handle_spawn(event, game_id, self.db,
                                  self.condition_registry)
```

- [ ] **Step 1.6: Run the test to verify it passes.**

```
python -m pytest tests/unit/capture/test_recorder.py::test_handle_spawn_event_propagates_timestamp_ms -v
```

Expected: PASS.

- [ ] **Step 1.7: Run full test suite.**

```
python -m pytest
```

Expected: green. Lua already emits `timestamp_ms`, so no integration test fixture needs updating; the wire format gained a field that consumers ignore unless they ask for it.

- [ ] **Step 1.8: Commit.**

```
git add python/spinlab/protocol.py python/spinlab/capture/reference.py tests/unit/capture/test_recorder.py
git commit -m "fix(capture): propagate spawn timestamp so clean_tail_ms is correct for segments with deaths"
```

---

## Task 2: Observability — recovery, session-end, and unlink logging (§4)

**Why:** `recover_paused_capture_run`, `_end_current_session`, and `hard_delete_capture_run` make silent decisions today. When a run goes missing, there's no trail to reconstruct what happened.

**Files:**
- Modify: `python/spinlab/db/capture_sessions.py:97-125` (recovery warnings + summary)
- Modify: `python/spinlab/db/capture_runs.py` (unlink warnings — see §4.3)
- Modify: `python/spinlab/capture/reference.py:156-166` (richer session-end log)
- Modify: `python/spinlab/capture/reference.py:392-407` (delete_capture_session unlink warning)
- Test: `tests/unit/capture/test_multi_session.py` (assert log emissions)

- [ ] **Step 2.1: Write a failing test for recovery warning when stranded drafts are discarded.**

Open `tests/unit/capture/test_multi_session.py`. Append:

```python
def test_recovery_logs_warning_when_discarding_stranded_drafts(db, caplog):
    """Two paused drafts for the same game — recovery keeps the newest and warns
    about the discarded one. No silent data loss."""
    import logging
    from datetime import UTC, datetime, timedelta

    db.upsert_game("smw", "SMW", "any%")
    older = "older_run"
    newer = "newer_run"
    db.create_capture_run(older, "smw", "Older", draft=True)
    db.create_capture_run(newer, "smw", "Newer", draft=True)
    # Force created_at ordering
    db.conn.execute(
        "UPDATE capture_runs SET created_at = ? WHERE id = ?",
        ((datetime.now(UTC) - timedelta(hours=1)).isoformat(), older),
    )
    db.conn.commit()

    with caplog.at_level(logging.WARNING, logger="spinlab.db.capture_sessions"):
        recovered = db.recover_paused_capture_run("smw")

    assert recovered == newer
    discard_warnings = [r for r in caplog.records if "discarding stranded draft" in r.getMessage().lower()]
    assert len(discard_warnings) == 1
    assert older in discard_warnings[0].getMessage()
```

- [ ] **Step 2.2: Run the test to verify it fails.**

```
python -m pytest tests/unit/capture/test_multi_session.py::test_recovery_logs_warning_when_discarding_stranded_drafts -v
```

Expected: FAIL — no warning emitted today; `len(discard_warnings) == 0`.

- [ ] **Step 2.3: Add the warning, plus a recovery-summary info log.**

Edit `python/spinlab/db/capture_sessions.py`. Add a module-level logger at the top (after the imports):

```python
import logging

logger = logging.getLogger(__name__)
```

Replace `recover_paused_capture_run` with:

```python
def recover_paused_capture_run(self, game_id: str) -> str | None:
    """Find the most recent draft (paused) capture_run for the game.

    Side effects:
    - Hard-deletes any older drafts for the same game (defensive — there
      should only be one paused run per game; if there are more, the
      oldest were stranded and are not recoverable into a coherent state).
      Each discard emits a warning so the loss is visible.
    - Marks any orphaned open sessions for the recovered run as crashed.

    Returns the recovered run id, or None if no draft exists.
    """
    rows = self.conn.execute(
        "SELECT id FROM capture_runs WHERE game_id = ? AND draft = 1 "
        "AND id NOT LIKE 'replay_%' "
        "ORDER BY created_at DESC",
        (game_id,),
    ).fetchall()
    if not rows:
        logger.info("recovery: no paused run for game=%s", game_id)
        return None
    recovered_id = rows[0][0]
    discarded = 0
    for row in rows[1:]:
        logger.warning(
            "recovery: discarding stranded draft capture_run=%s for game=%s "
            "(kept newer draft=%s)", row[0], game_id, recovered_id,
        )
        self.hard_delete_capture_run(row[0])
        discarded += 1
    crashed = self.mark_orphan_capture_sessions_crashed(recovered_id)
    logger.info(
        "recovery: kept_run=%s discarded_drafts=%d crashed_sessions=%d",
        recovered_id, discarded, crashed,
    )
    return recovered_id
```

- [ ] **Step 2.4: Run the test to verify it passes.**

```
python -m pytest tests/unit/capture/test_multi_session.py::test_recovery_logs_warning_when_discarding_stranded_drafts -v
```

Expected: PASS.

- [ ] **Step 2.5: Write a failing test for the richer session-end log.**

Append to `tests/unit/capture/test_multi_session.py`:

```python
def test_session_end_log_includes_ordinal_duration_segments(db, caplog):
    """When a capture session ends, log line includes ordinal, duration, and
    segment count to aid post-hoc debugging."""
    import logging, time
    from spinlab.capture.reference import ReferenceController

    db.upsert_game("smw", "SMW", "any%")
    db.create_capture_run("run_x", "smw", "X", draft=True)
    db.create_capture_session("sess_x", "run_x", 3, "/tmp/x.spinrec")
    # Add one segment so segment count > 0
    db.conn.execute(
        "INSERT INTO segments (id, game_id, level_number, start_type, start_ordinal, "
        "end_type, end_ordinal, capture_session_id, reference_id, created_at, updated_at) "
        "VALUES ('seg1', 'smw', 1, 'entrance', 0, 'goal', 0, 'sess_x', 'run_x', "
        "datetime('now'), datetime('now'))"
    )
    db.conn.commit()

    from tests.conftest import FakeTcpManager
    ctl = ReferenceController(db, FakeTcpManager(connected=False))
    ctl.recorder.capture_run_id = "run_x"
    ctl.recorder.current_capture_session_id = "sess_x"

    with caplog.at_level(logging.INFO, logger="spinlab.capture.reference"):
        ctl._end_current_session(end_reason="stopped")

    msgs = [r.getMessage() for r in caplog.records]
    end_msgs = [m for m in msgs if m.startswith("session: ended")]
    assert end_msgs, f"no session-end log; got: {msgs}"
    msg = end_msgs[0]
    assert "ordinal=3" in msg
    assert "segments=1" in msg
    assert "reason=stopped" in msg
    assert "duration_s=" in msg
```

- [ ] **Step 2.6: Run the test to verify it fails.**

```
python -m pytest tests/unit/capture/test_multi_session.py::test_session_end_log_includes_ordinal_duration_segments -v
```

Expected: FAIL — current log is just `"session: ended sess=%s reason=%s"`.

- [ ] **Step 2.7: Enrich the session-end log.**

Edit `python/spinlab/capture/reference.py:156-166`. Replace `_end_current_session` body up through the existing log line with:

```python
def _end_current_session(self, end_reason: str) -> None:
    """End the current capture session (if any). Run remains draft=1.

    Called from: stop_reference, handle_disconnect, stop_replay,
    handle_replay_finished, handle_replay_error.
    """
    sess_id = self.recorder.current_capture_session_id
    run_id = self.recorder.capture_run_id
    if sess_id:
        sess_row = self.db.get_capture_session(sess_id)
        seg_count = self.db.conn.execute(
            "SELECT COUNT(*) FROM segments WHERE capture_session_id = ?",
            (sess_id,),
        ).fetchone()[0]
        self.db.end_capture_session(sess_id, end_reason=end_reason)
        # Compute duration from started_at→now using the row we just updated.
        from datetime import UTC, datetime
        duration_s: float | None = None
        if sess_row and sess_row.get("started_at"):
            try:
                started = datetime.fromisoformat(sess_row["started_at"])
                duration_s = (datetime.now(UTC) - started).total_seconds()
            except ValueError:
                duration_s = None
        ordinal = sess_row["ordinal"] if sess_row else "?"
        dur_str = f"{duration_s:.1f}" if duration_s is not None else "?"
        logger.info(
            "session: ended sess=%s ordinal=%s duration_s=%s segments=%d reason=%s",
            sess_id, ordinal, dur_str, seg_count, end_reason,
        )
    # Surface run as paused (only if we had a run and it's still draft=1)
    if run_id:
        # ... rest of existing logic unchanged ...
```

> **Note:** Preserve the rest of the method exactly as it is — only the logging block above is new. Read the original method between lines 156 and ~180 of `reference.py` and transplant your changes accordingly. Do not delete the post-log code.

- [ ] **Step 2.8: Run the test to verify it passes.**

```
python -m pytest tests/unit/capture/test_multi_session.py::test_session_end_log_includes_ordinal_duration_segments -v
```

Expected: PASS.

- [ ] **Step 2.9: Add unlink-failure warnings.**

Replace silent `pass` in `delete_capture_session` (`python/spinlab/capture/reference.py:404-406`):

```python
        try:
            Path(sess["spinrec_path"]).unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Failed to unlink spinrec %s: %s", sess["spinrec_path"], exc)
```

Find the equivalent silent `pass` in `hard_delete_capture_run` (in `python/spinlab/db/capture_runs.py`) — search for `OSError` and add a matching `logger.warning(...)`. Add a module-level `logger = logging.getLogger(__name__)` to that file if missing.

- [ ] **Step 2.10: Run full test suite.**

```
python -m pytest
```

Expected: green.

- [ ] **Step 2.11: Commit.**

```
git add python/spinlab/db/capture_sessions.py python/spinlab/db/capture_runs.py python/spinlab/capture/reference.py tests/unit/capture/test_multi_session.py
git commit -m "feat(observability): log recovery decisions, session-end summary, and spinrec unlink failures"
```

---

## Task 3: Per-session segment count column (§3.1)

**Why:** Manage UI sessions table currently shows `—` as a placeholder for segment count. Without it, the salvage workflow is half-blind.

**Files:**
- Modify: `python/spinlab/db/capture_sessions.py:62-69` (add subquery)
- Modify: `python/spinlab/db/capture_sessions.py:11-19` (extend TypedDict)
- Modify: `frontend/src/types.ts:74-82` (add field)
- Modify: `frontend/src/manage.ts:156` (render value)
- Test: `tests/unit/capture/test_multi_session.py`

- [ ] **Step 3.1: Write a failing test.**

Append to `tests/unit/capture/test_multi_session.py`:

```python
def test_list_capture_sessions_includes_segment_count(db):
    db.upsert_game("smw", "SMW", "any%")
    db.create_capture_run("run_y", "smw", "Y", draft=True)
    db.create_capture_session("s1", "run_y", 1, "/tmp/1.spinrec")
    db.create_capture_session("s2", "run_y", 2, "/tmp/2.spinrec")
    # 2 segments in s1, 1 in s2
    for sid, csid in [("a", "s1"), ("b", "s1"), ("c", "s2")]:
        db.conn.execute(
            "INSERT INTO segments (id, game_id, level_number, start_type, "
            "start_ordinal, end_type, end_ordinal, capture_session_id, "
            "reference_id, created_at, updated_at) VALUES (?, 'smw', 1, "
            "'entrance', 0, 'goal', 0, ?, 'run_y', datetime('now'), datetime('now'))",
            (sid, csid),
        )
    db.conn.commit()

    sessions = db.list_capture_sessions_for_run("run_y")
    counts = {s["id"]: s["segment_count"] for s in sessions}
    assert counts == {"s1": 2, "s2": 1}
```

- [ ] **Step 3.2: Run the test to verify it fails.**

```
python -m pytest tests/unit/capture/test_multi_session.py::test_list_capture_sessions_includes_segment_count -v
```

Expected: FAIL — `KeyError: 'segment_count'`.

- [ ] **Step 3.3: Add the subquery and extend the TypedDict.**

In `python/spinlab/db/capture_sessions.py`, edit the `CaptureSessionRow` TypedDict to include the new field:

```python
class CaptureSessionRow(TypedDict):
    id: str
    capture_run_id: str
    ordinal: int
    started_at: str
    ended_at: str | None
    spinrec_path: str
    end_reason: str | None
    segment_count: int
```

Edit `list_capture_sessions_for_run`:

```python
def list_capture_sessions_for_run(self, capture_run_id: str) -> list[CaptureSessionRow]:
    rows = self.conn.execute(
        "SELECT s.id, s.capture_run_id, s.ordinal, s.started_at, s.ended_at, "
        "s.spinrec_path, s.end_reason, "
        "(SELECT COUNT(*) FROM segments WHERE capture_session_id = s.id) "
        "  AS segment_count "
        "FROM capture_sessions s "
        "WHERE s.capture_run_id = ? ORDER BY s.ordinal",
        (capture_run_id,),
    ).fetchall()
    return [dict(r) for r in rows]  # type: ignore[return-value]
```

Also extend `get_capture_session` to include `segment_count` (consumers may inspect a single session via that API):

```python
def get_capture_session(self, session_id: str) -> CaptureSessionRow | None:
    row = self.conn.execute(
        "SELECT s.id, s.capture_run_id, s.ordinal, s.started_at, s.ended_at, "
        "s.spinrec_path, s.end_reason, "
        "(SELECT COUNT(*) FROM segments WHERE capture_session_id = s.id) "
        "  AS segment_count "
        "FROM capture_sessions s WHERE s.id = ?",
        (session_id,),
    ).fetchone()
    if row is None:
        return None
    return dict(row)  # type: ignore[return-value]
```

- [ ] **Step 3.4: Run the test to verify it passes.**

```
python -m pytest tests/unit/capture/test_multi_session.py::test_list_capture_sessions_includes_segment_count -v
```

Expected: PASS.

- [ ] **Step 3.5: Update the TS type.**

Edit `frontend/src/types.ts:74-82`:

```typescript
export interface CaptureSession {
  id: string;
  capture_run_id: string;
  ordinal: number;
  started_at: string;
  ended_at: string | null;
  spinrec_path: string;
  end_reason: string | null;
  segment_count: number;
}
```

- [ ] **Step 3.6: Render the count in the manage UI.**

In `frontend/src/manage.ts`, find the placeholder `&#8212;` cell in `renderSessionsList` (line 156) and replace:

```typescript
      `<td>${s.segment_count}</td>` +
```

- [ ] **Step 3.7: Build and run frontend tests.**

```
cd frontend && npm run build && npm test && cd ..
```

Expected: build clean, tests green. The `api-contract.test.ts` should still pass — it accepts extra fields, but if it pins shape verify by skimming the failure if any.

- [ ] **Step 3.8: Run full Python test suite.**

```
python -m pytest
```

Expected: green.

- [ ] **Step 3.9: Commit.**

```
git add python/spinlab/db/capture_sessions.py frontend/src/types.ts frontend/src/manage.ts tests/unit/capture/test_multi_session.py
git commit -m "feat(manage): show per-session segment count in sessions table"
```

---

## Task 4: "Session" column on segments tab (§3.2)

**Why:** Segments already carry `capture_session_id`, but the manage segments table doesn't surface which session captured which segment. Salvage workflow needs this.

**Files:**
- Modify: `python/spinlab/db/capture_runs.py:135-148` (`get_segments_by_reference` — add LEFT JOIN to `capture_sessions`)
- Modify: `frontend/src/types.ts:194-207` (`ReferenceSegment.session_ordinal: number | null`)
- Modify: `frontend/src/manage.ts:117-140` (add the column header + cell)

- [ ] **Step 4.1: Read the existing query at `python/spinlab/db/capture_runs.py:135`.**

The current SQL selects directly from `segments WHERE reference_id = ?`. We need to LEFT JOIN `capture_sessions` and add `cs.ordinal AS session_ordinal`. Note the comment in the existing code about `state_path` being NULL until a future task — leave that path alone, only add the new join.

- [ ] **Step 4.2: Write a failing test.**

Append to `tests/unit/capture/test_multi_session.py`:

```python
def test_get_segments_by_reference_includes_session_ordinal(db):
    db.upsert_game("smw", "SMW", "any%")
    db.create_capture_run("run_z", "smw", "Z", draft=True)
    db.create_capture_session("s1", "run_z", 1, "/tmp/1.spinrec")
    db.create_capture_session("s2", "run_z", 2, "/tmp/2.spinrec")
    db.conn.execute(
        "INSERT INTO segments (id, game_id, level_number, start_type, "
        "start_ordinal, end_type, end_ordinal, capture_session_id, "
        "reference_id, created_at, updated_at) VALUES "
        "('a', 'smw', 1, 'entrance', 0, 'goal', 0, 's1', 'run_z', "
        "datetime('now'), datetime('now')), "
        "('b', 'smw', 1, 'entrance', 0, 'goal', 0, 's2', 'run_z', "
        "datetime('now'), datetime('now'))"
    )
    db.conn.commit()
    segs = db.get_segments_by_reference("run_z")
    by_id = {s["id"]: s for s in segs}
    assert by_id["a"]["session_ordinal"] == 1
    assert by_id["b"]["session_ordinal"] == 2
```

- [ ] **Step 4.3: Run the test to verify it fails.**

```
python -m pytest tests/unit/capture/test_multi_session.py::test_get_segments_by_reference_includes_session_ordinal -v
```

Expected: FAIL — KeyError on `session_ordinal`.

- [ ] **Step 4.4: Add the column to the query.**

In `python/spinlab/db/capture_runs.py:135-148`, replace `get_segments_by_reference` with:

```python
def get_segments_by_reference(self, reference_id: str) -> list[ReferenceSegmentRow]:
    # state_path is always NULL until a future task rewrites this to join
    # waypoint_save_states via start_waypoint_id.
    cur = self.conn.execute(
        """SELECT s.id, s.game_id, s.level_number, s.start_type, s.start_ordinal,
                  s.end_type, s.end_ordinal, s.description, s.active, s.ordinal,
                  s.reference_id, s.capture_session_id,
                  cs.ordinal AS session_ordinal,
                  NULL AS state_path
           FROM segments s
           LEFT JOIN capture_sessions cs ON s.capture_session_id = cs.id
           WHERE s.reference_id = ? AND s.active = 1
           ORDER BY s.ordinal""",
        (reference_id,),
    )
    actual_cols = [desc[0] for desc in cur.description]
    return [dict(zip(actual_cols, row)) for row in cur.fetchall()]  # type: ignore[return-value]
```

If `ReferenceSegmentRow` (TypedDict) is defined in this file, add `session_ordinal: int | None` to it.

- [ ] **Step 4.5: Run the test to verify it passes.**

```
python -m pytest tests/unit/capture/test_multi_session.py::test_get_segments_by_reference_includes_session_ordinal -v
```

Expected: PASS.

- [ ] **Step 4.6: Update the TS type.**

Edit `frontend/src/types.ts:194-207` (`ReferenceSegment` interface) — add:

```typescript
  session_ordinal: number | null;
```

- [ ] **Step 4.7: Render the column.**

In `frontend/src/manage.ts`, find the segment table rendering in `updateManage` (lines 117-140). Add a `<th>Session</th>` column to the existing header in `index.html` (search for `segment-body` ancestor table header to locate). Then in the row template, add a cell rendering `s.session_ordinal ?? "—"`:

```typescript
    tr.innerHTML =
      '<td>' + (s.session_ordinal ?? '—') + '</td>' +
      '<td><input class="segment-name-input" value="' +
      // ... rest unchanged
```

> Locate the matching `<th>` row in the index template (likely `frontend/index.html` or `python/spinlab/static/index.html` — check both). Add the new `<th>Session</th>` in the corresponding header row.

- [ ] **Step 4.8: Build and run frontend tests.**

```
cd frontend && npm run build && npm test && cd ..
```

Expected: green.

- [ ] **Step 4.9: Run full Python test suite.**

```
python -m pytest
```

Expected: green.

- [ ] **Step 4.10: Commit.**

```
git add python/spinlab/db/capture_runs.py frontend/src/types.ts frontend/src/manage.ts frontend/index.html python/spinlab/static/index.html tests/unit/capture/test_multi_session.py
git commit -m "feat(manage): show session ordinal column on segments tab"
```

> If `frontend/index.html` and `python/spinlab/static/index.html` are not both present, only stage the one that exists.

---

## Task 5: Tighten `sections_captured` TS type (§3.3)

**Why:** Frontend declares `sections_captured: number` but Python emits `int | None`. The papered-over `?? 0` works but the contract lies.

**Files:**
- Modify: `frontend/src/types.ts:163`
- Modify: any consumer that doesn't already handle null (most use `?? 0`)

- [ ] **Step 5.1: Edit the type.**

In `frontend/src/types.ts:163`:

```typescript
  sections_captured: number | null;
```

- [ ] **Step 5.2: Run TypeScript checks.**

```
cd frontend && npm run typecheck && cd ..
```

Expected: any consumer that does not handle `null` will error. Inspect failures.

- [ ] **Step 5.3: Fix any consumers that fail typecheck.**

Most likely `manage.ts:74` uses `?? 0` already, so no fix is needed. If anywhere reads `state.sections_captured` directly without coalescing, add `?? 0` (or display `—`).

- [ ] **Step 5.4: Run frontend tests.**

```
cd frontend && npm test && cd ..
```

Expected: green. If `api-contract.test.ts` pins the shape strictly, update it accordingly.

- [ ] **Step 5.5: Run full Python test suite.**

```
python -m pytest
```

Expected: green.

- [ ] **Step 5.6: Commit.**

```
git add frontend/src/types.ts frontend/src/api-contract.test.ts frontend/src/manage.ts
git commit -m "fix(types): sections_captured can be null — match Python's int | None"
```

> Stage only the files that actually changed.

---

## Task 6: Frontend handler tests for resume / save-and-finish / discard-run (§3.4)

**Why:** The new buttons added by the multi-session work have no Vitest coverage. Mock fetch, click each button, assert URL + body shape.

**Files:**
- Create: `frontend/src/manage.test.ts`

- [ ] **Step 6.1: Create the test file.**

```typescript
// frontend/src/manage.test.ts
import { describe, it, expect, vi, beforeEach } from "vitest";
import { initManageTab } from "./manage";

const mockFetch = vi.fn();
vi.stubGlobal("fetch", mockFetch);

beforeEach(() => {
  mockFetch.mockReset();
  document.body.innerHTML = `
    <select id="ref-select"></select>
    <button id="btn-ref-start"></button>
    <button id="btn-replay"></button>
    <button id="btn-ref-rename"></button>
    <button id="btn-ref-delete"></button>
    <button id="btn-resume"></button>
    <button id="btn-save-and-finish"></button>
    <button id="btn-discard-run"></button>
    <button id="btn-reset"></button>
    <input id="finalize-name" />
    <div id="paused-run-card"></div>
    <div id="paused-run-summary"></div>
    <div id="recording-indicator"></div>
    <div id="recording-seg-count"></div>
    <div id="cold-fill-banner"></div>
    <div id="reset-status"></div>
    <table><tbody id="segment-body"></tbody></table>
    <table><tbody id="sessions-body"></tbody></table>
  `;
  // Stub confirm so discard prompt doesn't block tests
  vi.stubGlobal("confirm", () => true);
  initManageTab();
  mockFetch.mockResolvedValue({
    ok: true,
    json: () => Promise.resolve({ status: "ok" }),
  });
});

describe("Resume button", () => {
  it("POSTs to /api/reference/resume with empty body", async () => {
    document.getElementById("btn-resume")!.click();
    await Promise.resolve();
    expect(mockFetch).toHaveBeenCalledWith("/api/reference/resume", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
  });
});

describe("Save & Finish button", () => {
  it("POSTs to /api/reference/save_and_finish with name from input", async () => {
    (document.getElementById("finalize-name") as HTMLInputElement).value = "My Run";
    document.getElementById("btn-save-and-finish")!.click();
    await Promise.resolve();
    expect(mockFetch).toHaveBeenCalledWith("/api/reference/save_and_finish", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: "My Run" }),
    });
  });

  it("falls back to 'Untitled' when name input is empty", async () => {
    (document.getElementById("finalize-name") as HTMLInputElement).value = "";
    document.getElementById("btn-save-and-finish")!.click();
    await Promise.resolve();
    const call = mockFetch.mock.calls[0];
    expect(JSON.parse(call[1].body)).toEqual({ name: "Untitled" });
  });
});

describe("Discard Run button", () => {
  it("POSTs to /api/reference/discard_run after confirm", async () => {
    document.getElementById("btn-discard-run")!.click();
    await Promise.resolve();
    expect(mockFetch).toHaveBeenCalledWith("/api/reference/discard_run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
  });

  it("does NOT POST when confirm is denied", async () => {
    vi.stubGlobal("confirm", () => false);
    document.getElementById("btn-discard-run")!.click();
    await Promise.resolve();
    expect(mockFetch).not.toHaveBeenCalled();
  });
});
```

- [ ] **Step 6.2: Run the new tests.**

```
cd frontend && npm test -- manage.test && cd ..
```

Expected: PASS for all five tests. If any fail because of DOM ordering or the second `vi.stubGlobal("confirm", ...)` not propagating, swap to `vi.spyOn(window, "confirm").mockReturnValue(false)` for that case.

- [ ] **Step 6.3: Run full frontend suite.**

```
cd frontend && npm test && cd ..
```

Expected: green.

- [ ] **Step 6.4: Commit.**

```
git add frontend/src/manage.test.ts
git commit -m "test(frontend): cover resume/save-and-finish/discard-run button handlers"
```

---

## Task 7: Empty-state pass on the manage page (§3.5)

**Why:** Easy to break first-run UX during a refactor. Verify the manage page reads correctly with: zero references / no paused run; one paused run / zero finalised; one finalised / one paused.

**Files:** No code changes expected. If copy needs a tweak, modify `frontend/src/manage.ts` and/or the HTML template.

- [ ] **Step 7.1: Reset the DB to a clean state.**

```
spinlab db reset
```

- [ ] **Step 7.2: Start the dashboard and inspect each empty/partial state in a browser.**

```
spinlab dashboard
```

Open `http://localhost:8000/manage` (or whatever the manage path is) and check:

1. **Zero references, no paused run.** "No references" placeholder visible. All buttons except "Start Reference" disabled. Sessions card hidden.
2. **One paused run, zero finalised.** Paused-run card visible with summary text. Sessions list populated with at least one row. Resume / Save & Finish / Discard buttons enabled.
3. **One finalised, one paused.** Reference dropdown has the finalised entry; paused-run card still visible. Activating the finalised reference works.

To set up state 2, start a reference run, capture one segment, then kill the dashboard and restart. To set up state 3, finalise a reference first, then repeat the paused setup.

- [ ] **Step 7.3: Document findings.**

If any state reads poorly, fix the copy or styling in `frontend/src/manage.ts` / the HTML template, run the build + tests:

```
cd frontend && npm run build && npm test && cd ..
python -m pytest
```

If no fixes were needed, no commit is required for this task — note the manual verification in the commit message of the next task.

- [ ] **Step 7.4: Commit (only if changes were made).**

```
git add frontend/src/manage.ts frontend/index.html python/spinlab/static/index.html
git commit -m "polish(manage): tighten empty-state and partial-state copy"
```

---

## Task 8: Always rebuild scheduler after activation (§2)

**Why:** `finalize_run` and `save_and_finish_run` skip `rebuild_all_states` when `seeded == 0`. But `set_active_capture_run` ran first, so the scheduler's notion of which segments belong to "the active reference" is stale. Skipping the rebuild is a micro-optimisation that produces silently wrong scheduling.

**Files:**
- Modify: `python/spinlab/capture/reference.py:262-263, 376-377` (rebuild unconditionally)
- Test: `tests/unit/capture/test_multi_session.py`

- [ ] **Step 8.1: Locate existing test that asserts the no-rebuild behaviour.**

```
grep -n "rebuild_all_states\|seeded == 0\|seeded.*scheduler" tests/unit/capture/test_multi_session.py tests/unit/capture/test_reference.py
```

If a test exists that asserts `rebuild_all_states` is NOT called when seeded is 0, it needs inverting. Otherwise we add a fresh test.

- [ ] **Step 8.2: Write the new test asserting rebuild fires regardless of seed count.**

Append to `tests/unit/capture/test_multi_session.py`:

```python
def test_finalize_rebuilds_scheduler_even_when_zero_segments(db):
    """Activating a reference invalidates scheduler state regardless of how many
    new attempts were seeded. Rebuild must fire."""
    from spinlab.capture.reference import ReferenceController

    db.upsert_game("smw", "SMW", "any%")
    db.create_capture_run("run_e", "smw", "Empty", draft=True)
    db.create_capture_session("s_e", "run_e", 1, "/tmp/e.spinrec")

    from tests.conftest import FakeTcpManager
    class RecordingScheduler:
        def __init__(self): self.rebuild_calls = 0
        def rebuild_all_states(self): self.rebuild_calls += 1
    sched = RecordingScheduler()

    ctl = ReferenceController(db, FakeTcpManager(connected=False))
    ctl.paused_run_id = "run_e"

    import asyncio
    asyncio.run(ctl.finalize_run(name="Empty Run", scheduler=sched))

    assert sched.rebuild_calls == 1, (
        "scheduler must rebuild after set_active_capture_run, even with zero seeded attempts"
    )
```

- [ ] **Step 8.3: Run the test to verify it fails.**

```
python -m pytest tests/unit/capture/test_multi_session.py::test_finalize_rebuilds_scheduler_even_when_zero_segments -v
```

Expected: FAIL — `rebuild_calls == 0` because the existing guard skips on `seeded == 0`.

- [ ] **Step 8.4: Make the fix in `finalize_run`.**

Edit `python/spinlab/capture/reference.py:262-263`:

```python
        seeded = _seed_reference_attempts(self.db, run_id, timing_rows)
        # Always rebuild after activation: set_active_capture_run changed which
        # reference the scheduler should be reasoning about, regardless of whether
        # this finalize added new attempts.
        if scheduler:
            scheduler.rebuild_all_states()
```

- [ ] **Step 8.5: Make the same fix in `save_and_finish_run`.**

Edit `python/spinlab/capture/reference.py:376-377`:

```python
        if scheduler:
            scheduler.rebuild_all_states()
```

(Drop the `if seeded` guard.)

- [ ] **Step 8.6: Run the new test to verify it passes.**

```
python -m pytest tests/unit/capture/test_multi_session.py::test_finalize_rebuilds_scheduler_even_when_zero_segments -v
```

Expected: PASS.

- [ ] **Step 8.7: Run full test suite. Update any test that asserted no-rebuild when seeded == 0.**

```
python -m pytest
```

If any test fails because it pinned the old behaviour ("scheduler.rebuild_all_states was not called"), invert that assertion — the new behaviour is unconditional. Re-run.

- [ ] **Step 8.8: Commit.**

```
git add python/spinlab/capture/reference.py tests/unit/capture/test_multi_session.py
git commit -m "fix(reference): always rebuild scheduler after set_active_capture_run

The active reference changed; scheduler state derived from the previous
active reference is stale regardless of whether new attempts were seeded."
```

---

## Task 9: Replay ↔ paused-run integration test (§5)

**Why:** The `id NOT LIKE 'replay_%'` filter is the most fragile part of the merged work. Unit-tested but not end-to-end tested. Lock it down.

**Files:**
- Create: `tests/integration/test_replay_paused_recovery.py` (or extend `test_crash_recovery.py`)

- [ ] **Step 9.1: Decide on placement.**

Read `tests/integration/test_crash_recovery.py` first:

```
wc -l tests/integration/test_crash_recovery.py
```

If it has fewer than ~250 lines and the existing fixtures cover dashboard restart, append the new test there. Otherwise create a fresh file `tests/integration/test_replay_paused_recovery.py` and reuse the fixtures (likely from `conftest.py`).

- [ ] **Step 9.2: Write the test.**

Skeleton (adapt to whatever fixtures the chosen file already uses):

```python
import pytest

@pytest.mark.slow
def test_paused_run_survives_replay_then_dashboard_restart(spinlab_dashboard, db):
    """User has paused run A. Plays a replay of finalised reference B. Replay
    finishes. Dashboard restarts. Recovery picks A back up; B's segments do not
    leak into A; the replay-derived capture_run row is gone (filtered by
    id NOT LIKE 'replay_%')."""
    # 1. Create a paused run A with one segment captured.
    db.upsert_game("smw", "SMW", "any%")
    db.create_capture_run("run_A", "smw", "A paused", draft=True)
    db.create_capture_session("sA1", "run_A", 1, "/tmp/A1.spinrec")
    db.conn.execute(
        "INSERT INTO segments (id, game_id, level_number, start_type, start_ordinal, "
        "end_type, end_ordinal, capture_session_id, reference_id, "
        "created_at, updated_at) VALUES "
        "('seg_A', 'smw', 1, 'entrance', 0, 'goal', 0, 'sA1', 'run_A', "
        "datetime('now'), datetime('now'))"
    )
    # 2. Create a finalised reference B.
    db.create_capture_run("run_B", "smw", "B finalised", draft=False)
    db.conn.execute(
        "UPDATE capture_runs SET active = 0, draft = 0 WHERE id = 'run_B'"
    )
    db.conn.commit()

    # 3. Simulate replay: a replay_xxx capture_run row exists with one segment.
    db.create_capture_run("replay_abc", "smw", "Replay X", draft=True)
    db.create_capture_session("sR", "replay_abc", 1, "/tmp/R.spinrec")
    db.conn.execute(
        "INSERT INTO segments (id, game_id, level_number, start_type, start_ordinal, "
        "end_type, end_ordinal, capture_session_id, reference_id, "
        "created_at, updated_at) VALUES "
        "('seg_R', 'smw', 99, 'entrance', 0, 'goal', 0, 'sR', 'replay_abc', "
        "datetime('now'), datetime('now'))"
    )
    db.conn.commit()

    # 4. Simulate dashboard restart by calling recovery directly.
    recovered = db.recover_paused_capture_run("smw")

    # Assertions:
    assert recovered == "run_A", "recovery picked replay run instead of paused A"
    a_segs = db.conn.execute(
        "SELECT id FROM segments WHERE reference_id = 'run_A'"
    ).fetchall()
    assert {r[0] for r in a_segs} == {"seg_A"}, "B's segments leaked into A"
    # Replay-derived run is left as-is (not auto-deleted by recovery — that's a
    # separate cleanup pass), but is NOT promoted to paused state.
    a_state = db.conn.execute(
        "SELECT id FROM capture_runs WHERE id = 'replay_abc'"
    ).fetchone()
    assert a_state is not None, (
        "replay_abc was hard-deleted by recovery — but the spec says replay drafts "
        "accumulate until explicitly discarded"
    )
```

> If `recover_paused_capture_run` is changed by Task 14 (partial unique index) such that two simultaneous drafts can't exist, this test still passes because the `replay_abc` row has the prefix and the index excludes it.

- [ ] **Step 9.3: Run the test.**

```
python -m pytest tests/integration/test_replay_paused_recovery.py -v
```

(Or whichever path you used.)

Expected: PASS without code changes — the existing logic should already handle this. If it FAILS, the test has caught a real regression and you need to investigate before continuing.

- [ ] **Step 9.4: Run full test suite.**

```
python -m pytest
```

Expected: green.

- [ ] **Step 9.5: Commit.**

```
git add tests/integration/test_replay_paused_recovery.py
git commit -m "test: integration coverage for paused-run survival across replay + restart"
```

---

## Task 10: Rename `attempts.session_id` → `attempts.parent_id` (§6.1)

**Why:** Three meanings of "session" today: practice sessions, polymorphic parent on attempts, capture sessions. The `attempts.session_id` use is the one that's *not* a session in either of the other senses. Greenfield rename — `_init_schema` will detect column drift and rebuild the table, so a `spinlab db reset` is required after the change but no migration is needed.

**WARNING:** This task wipes existing `attempts` data on the next dashboard start. If you have a paused run you want to keep, finalise it first.

**Files:**
- Modify: `python/spinlab/db/core.py:56-71` (SCHEMA), `:138-139` (index), `:186-191` (`_expected_columns`)
- Modify: `python/spinlab/db/attempts.py` (all SQL referencing `session_id`)
- Modify: `python/spinlab/models.py:155-157` (`Attempt.session_id` → `Attempt.parent_id`)
- Modify: `python/spinlab/capture/reference.py:343, 358` (`session_id=` → `parent_id=`, `attempt.session_id` → `attempt.parent_id`)
- Modify: `python/spinlab/practice.py:193`, `python/spinlab/speed_run.py:244` (constructor kwargs)
- Modify: tests that construct `Attempt(session_id=...)` directly

- [ ] **Step 10.1: Find all `Attempt(...)` construction sites.**

```
grep -rn "Attempt(" python/spinlab tests | grep -v "AttemptRow\|AttemptSource\|AttemptResultEvent\|AttemptRecord\|AttemptInvalidatedEvent"
```

Make a list of the files and line numbers. Each one needs `session_id=` → `parent_id=` (kwarg) or positional adjustment.

- [ ] **Step 10.2: Find all `attempt.session_id` and `.session_id =` accesses on Attempt instances.**

```
grep -rn "attempt\.session_id\|\.session_id =" python/spinlab
```

Filter mentally to only the ones that are on Attempt objects (not on practice sessions, speed runs, capture sessions, or dataclasses unrelated to attempts).

- [ ] **Step 10.3: Find all SQL referencing the column.**

```
grep -rn "session_id" python/spinlab/db/
```

Expected sites (verify each — some are on `capture_sessions` or `sessions` tables and must NOT change):
- `python/spinlab/db/attempts.py:54` (INSERT)
- `python/spinlab/db/attempts.py:85, 87-89` (`get_segment_attempt_count` SQL)
- `python/spinlab/db/attempts.py:105` (`get_recent_attempts` WHERE clause)
- `python/spinlab/db/attempts.py:158` (`get_last_practice_attempt` SQL)
- `python/spinlab/db/core.py:59` (SCHEMA), `:139` (index), `:187` (`_expected_columns`)

The function *parameter* names (e.g., `def get_segment_attempt_count(self, segment_id: str, session_id: str)`) can stay — at the call site, `session_id` is the right semantic name (a practice session id). Only the *column* name changes.

- [ ] **Step 10.4: Update the schema.**

`python/spinlab/db/core.py:56-71`:

```sql
CREATE TABLE IF NOT EXISTS attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  segment_id TEXT NOT NULL REFERENCES segments(id),
  parent_id TEXT NOT NULL,
  completed INTEGER NOT NULL,
  time_ms INTEGER,
  strat_version INTEGER NOT NULL,
  source TEXT DEFAULT 'practice',
  deaths INTEGER DEFAULT 0,
  clean_tail_ms INTEGER,
  observed_start_conditions TEXT,
  observed_end_conditions TEXT,
  invalidated INTEGER DEFAULT 0,
  chosen_allocator TEXT,
  created_at TEXT NOT NULL
);
```

`python/spinlab/db/core.py:139`:

```sql
CREATE INDEX IF NOT EXISTS idx_attempts_parent ON attempts(parent_id);
```

`python/spinlab/db/core.py:186-191` — replace `"session_id"` with `"parent_id"` in the `attempts` set inside `_expected_columns`.

- [ ] **Step 10.5: Update `Attempt` model.**

`python/spinlab/models.py:155-157`:

```python
@dataclass
class Attempt:
    segment_id: str
    parent_id: str
    completed: bool
    # ... rest unchanged
```

- [ ] **Step 10.6: Update all SQL in `db/attempts.py`.**

Mechanical rename of `session_id` → `parent_id` *only on the `attempts` table*. Function parameter names can stay as `session_id` for caller-side semantic clarity. Example for `log_attempt`:

```python
def log_attempt(self, attempt: Attempt) -> int:
    cur = self.conn.execute(
        """INSERT INTO attempts
           (segment_id, parent_id, completed, time_ms,
            strat_version, source, deaths, clean_tail_ms,
            observed_start_conditions, observed_end_conditions, invalidated,
            chosen_allocator, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (attempt.segment_id, attempt.parent_id, int(attempt.completed),
         attempt.time_ms,
         attempt.strat_version, attempt.source,
         attempt.deaths, attempt.clean_tail_ms,
         attempt.observed_start_conditions, attempt.observed_end_conditions,
         int(attempt.invalidated),
         attempt.chosen_allocator,
         attempt.created_at.isoformat()),
    )
    self.conn.commit()
    return cur.lastrowid  # type: ignore[return-value]
```

For `get_segment_attempt_count`:

```python
def get_segment_attempt_count(self, segment_id: str, session_id: str) -> int:
    """Count attempts on a segment in a specific practice session.

    Note: ``session_id`` here is whatever populates ``attempts.parent_id`` —
    a practice session id, capture run id, or speed-run id, depending on call site.
    """
    row = self.conn.execute(
        "SELECT COUNT(*) as cnt FROM attempts "
        "WHERE segment_id = ? AND parent_id = ?",
        (segment_id, session_id),
    ).fetchone()
    return row["cnt"]
```

For `get_recent_attempts`:

```python
        if session_id:
            where += " AND a.parent_id = ?"
            params.append(session_id)
```

For `get_last_practice_attempt`:

```python
def get_last_practice_attempt(self, session_id: str) -> int | None:
    row = self.conn.execute(
        "SELECT id FROM attempts WHERE parent_id = ? "
        "ORDER BY id DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    return row[0] if row else None
```

Also update the `RecentAttemptRow` TypedDict (`python/spinlab/db/attempts.py:20-37`): replace `session_id: str` with `parent_id: str`.

- [ ] **Step 10.7: Update `Attempt(...)` constructors.**

For each site found in Step 10.1, replace `session_id=...` with `parent_id=...`. Sites:
- `python/spinlab/practice.py:193`
- `python/spinlab/speed_run.py:244`
- `python/spinlab/capture/reference.py:343` (the `Attempt(session_id=run_id, ...)` call inside `save_and_finish_run`)
- Tests that construct `Attempt(session_id=...)` (search and update)

Also update `python/spinlab/capture/reference.py:358`: `attempt.session_id` → `attempt.parent_id` (inside the inline INSERT in `save_and_finish_run`).

- [ ] **Step 10.8: Reset the DB and run the suite.**

```
spinlab db reset
python -m pytest
```

Expected: green. The schema rebuild-on-mismatch will drop and recreate `attempts` on first DB connection.

- [ ] **Step 10.9: Frontend tests.**

```
cd frontend && npm test && cd ..
```

Expected: green. The TS layer doesn't see the column name — it reads `recent` rows and uses `segment_id`, `time_ms`, etc. If the API contract test pinned `session_id`, update it.

- [ ] **Step 10.10: Commit.**

```
git add python/spinlab/db/core.py python/spinlab/db/attempts.py python/spinlab/models.py python/spinlab/capture/reference.py python/spinlab/practice.py python/spinlab/speed_run.py tests/
git commit -m "refactor(db): rename attempts.session_id → attempts.parent_id

The column has always been polymorphic (practice session / capture run /
speed-run id). 'session_id' overloaded a word that already means two
other things in this codebase. Greenfield rename — drop-on-mismatch
rebuilds the table on next start; existing attempts data is wiped."
```

---

## Task 11: Split `NoPausedRunError` from `NotInReferenceError` (§6.2)

**Why:** `NotInReferenceError` is currently raised both for "wrong mode" and "no paused run exists." Splitting clarifies the API contract.

**Files:**
- Modify: `python/spinlab/errors.py` (add new class)
- Modify: `python/spinlab/capture/reference.py:221, 256, 300, 385` (4 sites that mean "no paused run")

- [ ] **Step 11.1: Write a failing test.**

Append to `tests/unit/test_errors.py` (or extend the existing parametrize):

```python
def test_no_paused_run_error_distinct_from_not_in_reference():
    from spinlab.errors import NoPausedRunError, NotInReferenceError
    assert NoPausedRunError is not NotInReferenceError
    err = NoPausedRunError()
    assert err.http_code == 409
    assert err.detail == "no_paused_run"
```

Append to `tests/unit/capture/test_multi_session.py`:

```python
def test_finalize_raises_no_paused_run_error_when_no_run(db):
    from spinlab.capture.reference import ReferenceController
    from spinlab.errors import NoPausedRunError
    import asyncio
    from tests.conftest import FakeTcpManager
    ctl = ReferenceController(db, FakeTcpManager(connected=False))
    ctl.paused_run_id = None
    with pytest.raises(NoPausedRunError):
        asyncio.run(ctl.finalize_run(name="x", scheduler=None))
```

- [ ] **Step 11.2: Run the tests to verify they fail.**

```
python -m pytest tests/unit/test_errors.py tests/unit/capture/test_multi_session.py -v -k "no_paused"
```

Expected: FAIL — class doesn't exist.

- [ ] **Step 11.3: Add the class.**

In `python/spinlab/errors.py`, after `NotInReferenceError`:

```python
class NoPausedRunError(ActionError):
    """No paused capture run exists for the requested operation."""
    http_code = 409
    detail = "no_paused_run"
```

- [ ] **Step 11.4: Update the four "no paused run" call sites.**

In `python/spinlab/capture/reference.py`, replace `raise NotInReferenceError()` with `raise NoPausedRunError()` at:

- Line 220-221 (`resume_reference`: `if not self.paused_run_id`)
- Line 255-256 (`finalize_run`: `if not self.paused_run_id`)
- Line 299-300 (`save_and_finish_run`: post-`_end_current_session` check `if not run_id`)
- Line 384-385 (`discard_run`: `if not self.paused_run_id`)

Add `NoPausedRunError` to the import block at the top of `reference.py`.

> Lines 243, 292 stay as `NotInReferenceError` — those are the genuine "wrong mode" cases. Line 396 (`if not sess` in `delete_capture_session`) should change to a session-not-found error — see Task 14 for that one; for now leave it as is to keep this task atomic.

- [ ] **Step 11.5: Run the tests to verify they pass.**

```
python -m pytest tests/unit/test_errors.py tests/unit/capture/test_multi_session.py -v
```

Expected: green.

- [ ] **Step 11.6: Update existing tests that pinned the old behaviour.**

```
grep -n "NotInReferenceError" tests/
```

Any test that expected `NotInReferenceError` for a "no paused run" case (resume / finalize / discard) needs updating to `NoPausedRunError`. Likely candidates: `tests/unit/capture/test_reference.py:74` and similar.

- [ ] **Step 11.7: Run full suite.**

```
python -m pytest
```

Expected: green.

- [ ] **Step 11.8: Commit.**

```
git add python/spinlab/errors.py python/spinlab/capture/reference.py tests/
git commit -m "refactor(errors): split NoPausedRunError out of NotInReferenceError

Disambiguates 'wrong mode' from 'no paused run exists' in the API contract."
```

---

## Task 12: Drop `RunPendingError` alias (§6.3)

**Why:** `errors.py` has `RunPendingError = DraftPendingError` with a comment promising future migration. The migration didn't happen. Complete the rename and drop the alias.

**Files:**
- Modify: `python/spinlab/errors.py:39-41` (drop alias + comment)
- Modify: `python/spinlab/capture/reference.py:30, 195, 418` (import + raise sites)
- Modify: `tests/unit/capture/test_reference.py:19, 47`, `tests/unit/capture/test_multi_session.py:7, 146`

**Decision:** the spec chose `DraftPendingError` as the canonical name. We rename callers, not the class.

- [ ] **Step 12.1: Find all `RunPendingError` references.**

```
grep -rn "RunPendingError" python/spinlab tests
```

- [ ] **Step 12.2: Replace each `RunPendingError` with `DraftPendingError` at:**

- `python/spinlab/capture/reference.py:30` (import)
- `python/spinlab/capture/reference.py:195` (raise in `start_reference`)
- `python/spinlab/capture/reference.py:418` (raise in `start_replay`)
- `tests/unit/capture/test_reference.py:19, 47`
- `tests/unit/capture/test_multi_session.py:7, 146`

(All mechanical s/RunPendingError/DraftPendingError/.)

- [ ] **Step 12.3: Drop the alias.**

In `python/spinlab/errors.py`, delete lines 39-41 (the comment + `RunPendingError = DraftPendingError` line).

- [ ] **Step 12.4: Run the full suite.**

```
python -m pytest
```

Expected: green. If anything still imports `RunPendingError`, the test will fail with `ImportError`; track it down with grep and replace.

- [ ] **Step 12.5: Commit.**

```
git add python/spinlab/errors.py python/spinlab/capture/reference.py tests/
git commit -m "refactor(errors): drop RunPendingError alias; standardise on DraftPendingError"
```

---

## Task 13: Partial unique index for one-paused-run-per-game (§6.4)

**Why:** Today the "one paused run per game" invariant relies on `recover_paused_capture_run` cleaning up. Make it impossible at the DB level.

**Files:**
- Modify: `python/spinlab/db/core.py` (add to SCHEMA)
- Test: `tests/unit/capture/test_multi_session.py`

- [ ] **Step 13.1: Write a failing test.**

Append to `tests/unit/capture/test_multi_session.py`:

```python
def test_two_paused_drafts_for_same_game_violate_unique_index(db):
    """Belt-and-suspenders constraint: at most one non-replay draft per game."""
    import sqlite3
    db.upsert_game("smw", "SMW", "any%")
    db.create_capture_run("run_1", "smw", "1", draft=True)
    with pytest.raises(sqlite3.IntegrityError):
        db.create_capture_run("run_2", "smw", "2", draft=True)


def test_replay_drafts_can_coexist_with_paused_run(db):
    """The unique index excludes replay_% IDs, so a replay draft does NOT
    collide with a real paused run."""
    db.upsert_game("smw", "SMW", "any%")
    db.create_capture_run("run_real", "smw", "Real", draft=True)
    # Should not raise:
    db.create_capture_run("replay_xx", "smw", "Replay", draft=True)
```

- [ ] **Step 13.2: Run the tests to verify the first fails.**

```
python -m pytest tests/unit/capture/test_multi_session.py::test_two_paused_drafts_for_same_game_violate_unique_index tests/unit/capture/test_multi_session.py::test_replay_drafts_can_coexist_with_paused_run -v
```

Expected: first FAILS (no constraint), second PASSES.

- [ ] **Step 13.3: Add the index to SCHEMA.**

In `python/spinlab/db/core.py`, add to the SCHEMA string (after the existing `idx_segments_capture_session` index, around line 143):

```sql
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_paused_run_per_game
  ON capture_runs(game_id)
  WHERE draft = 1 AND id NOT LIKE 'replay_%';
```

- [ ] **Step 13.4: Reset the DB.**

```
spinlab db reset
```

(Required because partial indexes on existing tables aren't picked up by `IF NOT EXISTS` if the predicate changes — and to make sure no existing data violates the new constraint.)

- [ ] **Step 13.5: Run the tests to verify they both pass.**

```
python -m pytest tests/unit/capture/test_multi_session.py::test_two_paused_drafts_for_same_game_violate_unique_index tests/unit/capture/test_multi_session.py::test_replay_drafts_can_coexist_with_paused_run -v
```

Expected: green.

- [ ] **Step 13.6: Run full suite.**

```
python -m pytest
```

Expected: green. The recovery code's "delete older drafts" path now never fires in normal operation; the warning from Task 2 should still log for the rare case where stranded replay-prefixed rows accumulate.

- [ ] **Step 13.7: Commit.**

```
git add python/spinlab/db/core.py tests/unit/capture/test_multi_session.py
git commit -m "feat(db): partial unique index enforces one-paused-run-per-game"
```

---

## Task 14: `delete_capture_session` active-recording guard (§6.5)

**Why:** `delete_capture_session` accepts any `session_id`. If the recorder is mid-segment in that session, the next segment-close hits an FK violation. Add an explicit guard.

**Files:**
- Modify: `python/spinlab/errors.py` (add `SessionInUseError`)
- Modify: `python/spinlab/capture/reference.py:392-409` (add guard)
- Test: `tests/unit/capture/test_multi_session.py`

- [ ] **Step 14.1: Write the failing test.**

Append to `tests/unit/capture/test_multi_session.py`:

```python
def test_delete_active_capture_session_raises_session_in_use(db):
    """If the recorder is currently writing into the session, deletion must
    raise SessionInUseError instead of leaving a dangling FK."""
    from spinlab.capture.reference import ReferenceController
    from spinlab.errors import SessionInUseError
    import asyncio
    db.upsert_game("smw", "SMW", "any%")
    db.create_capture_run("run_d", "smw", "D", draft=True)
    db.create_capture_session("active_sess", "run_d", 1, "/tmp/d.spinrec")

    from tests.conftest import FakeTcpManager
    ctl = ReferenceController(db, FakeTcpManager(connected=False))
    ctl.recorder.capture_run_id = "run_d"
    ctl.recorder.current_capture_session_id = "active_sess"

    with pytest.raises(SessionInUseError):
        asyncio.run(ctl.delete_capture_session("active_sess"))
```

- [ ] **Step 14.2: Run the test to verify it fails.**

```
python -m pytest tests/unit/capture/test_multi_session.py::test_delete_active_capture_session_raises_session_in_use -v
```

Expected: FAIL — class doesn't exist or no guard.

- [ ] **Step 14.3: Add the new error class.**

In `python/spinlab/errors.py`, after `SessionDeleteAfterFinalizeError`:

```python
class SessionInUseError(ActionError):
    """Cannot delete a capture session while it is being recorded into."""
    http_code = 409
    detail = "session_in_use"
```

- [ ] **Step 14.4: Add the guard.**

In `python/spinlab/capture/reference.py:392-409`, replace `delete_capture_session` body's opening:

```python
async def delete_capture_session(self, session_id: str) -> ActionResult:
    """Delete a single capture session. Only allowed while run is paused and
    the session is not currently being recorded into."""
    if self.recorder.current_capture_session_id == session_id:
        raise SessionInUseError()
    sess = self.db.get_capture_session(session_id)
    if not sess:
        raise NotInReferenceError()
    # ... rest unchanged ...
```

Add `SessionInUseError` to the imports at the top of `reference.py`.

- [ ] **Step 14.5: Run the test to verify it passes.**

```
python -m pytest tests/unit/capture/test_multi_session.py::test_delete_active_capture_session_raises_session_in_use -v
```

Expected: PASS.

- [ ] **Step 14.6: Add error mapping test.**

Append to `tests/unit/test_errors.py`:

```python
def test_session_in_use_error_shape():
    from spinlab.errors import SessionInUseError
    err = SessionInUseError()
    assert err.http_code == 409
    assert err.detail == "session_in_use"
```

If the file uses parametrize for shape tests, add `(SessionInUseError, 409, "session_in_use")` to the table.

- [ ] **Step 14.7: Run full suite.**

```
python -m pytest
```

Expected: green.

- [ ] **Step 14.8: Commit.**

```
git add python/spinlab/errors.py python/spinlab/capture/reference.py tests/
git commit -m "feat(api): SessionInUseError when deleting a capture session mid-recording"
```

---

## Final Verification

After all tasks, run the full suite one more time and verify the invariants the spec promised:

- [ ] **Step F.1: Full Python suite.**

```
python -m pytest
```

Expected: green.

- [ ] **Step F.2: Full frontend suite.**

```
cd frontend && npm test && cd ..
```

Expected: green.

- [ ] **Step F.3: Spot-check observability.**

Manually trigger a recovery scenario (start a reference run, stop the dashboard, restart) and grep the log:

```
spinlab dashboard
# (start a reference, capture a segment, kill, restart)
grep -E "recovery:|session: ended" "$(spinlab config show --key data_dir)/spinlab.log" | tail -10
```

Expected output includes `recovery: kept_run=...`, `session: ended sess=... ordinal=N duration_s=... segments=N reason=...`.

- [ ] **Step F.4: Manual UI verification.**

Open the manage page in the dashboard. Confirm:
- Sessions table shows segment counts (not `—`).
- Segments tab has a "Session" column.
- Resume / Save & Finish / Discard buttons work for a paused run.
- Empty/partial states from Task 7 still read well.

- [ ] **Step F.5: Update follow-ups file.**

Edit `multi-session-followups.txt` to mark items 1, 4, 5, 6, 8, 9, 10, 12, 15, 24, 33, 34, 36 as done — strike them through or move to a "Done" section. Optionally annotate each with the commit hash.

- [ ] **Step F.6: Commit the follow-ups update.**

```
git add multi-session-followups.txt
git commit -m "docs: mark multi-session follow-ups completed in this pass"
```
