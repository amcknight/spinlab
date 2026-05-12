# Capture + RetroArch Encapsulation Pass — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Slim `capture/reference.py` (660 lines) and `retroarch/orchestrator.py` (475 lines) by extracting four focused units, each with its own unit test.

**Architecture:** Four independent extractions, one commit each. Order: finalizer (pure SQL extraction) → fill_gap controller (state-pair extraction) → movies controller (state-block extraction in orchestrator) → wiring (mechanical move of `build_orchestrator`). Each commit must keep the full test suite green.

**Tech Stack:** Python 3.11+, dataclasses, asyncio, pytest, SQLite. No new dependencies.

**Spec:** [docs/superpowers/specs/2026-05-11-capture-retroarch-encapsulation-design.md](../specs/2026-05-11-capture-retroarch-encapsulation-design.md)

---

## Constraints

- **TDD throughout.** New test first → run to verify it fails → implement → run to verify it passes → commit.
- **Full pytest before starting AND after every task.** Per `feedback_fix_preexisting_failures.md` and CLAUDE.md "Merging Branches": `python -m pytest` (unfiltered) must be green at every checkpoint. Fix pre-existing failures before starting Task 1 if any exist.
- **No behavior changes.** This is pure refactor. If a test had to change in a way that asserts different behavior (vs. asserting the same behavior at a new call site), stop and re-evaluate.
- **One file moved/created per task.** No bundling. Reverts must be surgical.
- **Use `git mv` for any file relocations.** Preserves blame.
- **Edit existing files in place.** Don't rewrite whole files when an `Edit` would do.

## Baseline

Before Task 1, run the full suite:

```
python -m pytest
```

Expected: 804 passed (or current baseline) in ~50s. If any test fails, fix or document pre-existing failures before starting.

## File Map

Files created in this plan:

| File | Lines (target) | Responsibility |
|---|---|---|
| `python/spinlab/capture/finalizer.py` | ~110 | `atomic_save_and_finish_run` function — atomic 5-step transaction |
| `python/spinlab/capture/fill_gap.py` | ~55 | `FillGapController` class — fill-gap mode state machine |
| `python/spinlab/retroarch/movies.py` | ~95 | `MovieController` class — record/playback lifecycle, fast-forward toggle |
| `python/spinlab/retroarch/wiring.py` | ~95 | `build_orchestrator` factory (relocated verbatim) |
| `tests/unit/capture/test_finalizer.py` | new | Commit + rollback unit tests |
| `tests/unit/capture/test_fill_gap.py` | new | FillGapController unit tests |
| `tests/unit/retroarch/test_movies.py` | new | MovieController unit tests |

Files modified:

| File | Change |
|---|---|
| `python/spinlab/capture/reference.py` | Drop 100-line transaction body (Task 1); drop 2 fields + 2 methods (Task 2). Net: 660 → ~520 lines. |
| `python/spinlab/session_manager.py` | Construct + wire `FillGapController` (Task 2); update `_handle_spawn` dispatch. |
| `python/spinlab/retroarch/orchestrator.py` | Replace movie state + 4 handlers with `MovieController` delegations (Task 3); lose `build_orchestrator` (Task 4). Net: 475 → ~310 lines. |
| `python/spinlab/dashboard.py` | Update import path for `build_orchestrator` (Task 4). |
| `tests/unit/retroarch/test_orchestrator.py` | Update import path (Task 4). |

---

## Task 1 — Extract `atomic_save_and_finish_run`

**Files:**
- Create: `python/spinlab/capture/finalizer.py`
- Create: `tests/unit/capture/test_finalizer.py`
- Modify: `python/spinlab/capture/reference.py` (replace transaction body in `save_and_finish_run`)

### Task 1.1: Write the rollback test first

This test runs *against the current production code* before extraction. It verifies the rollback contract that we're about to relocate — if it fails on current code, the extraction would mask a real bug.

- [ ] **Step 1: Create the test file**

Create `tests/unit/capture/test_finalizer.py` with the rollback test against current production:

```python
"""Tests for atomic_save_and_finish_run.

Task 1.1 writes the test against the current inline implementation in
ReferenceController.save_and_finish_run — verifies the rollback contract
before extraction. Task 1.3 re-points the test at the extracted function.
"""
from __future__ import annotations

import pytest

from spinlab.models import Mode, Status
from spinlab.protocol import ReferenceStopCmd


@pytest.mark.asyncio
async def test_rollback_on_mid_transaction_failure(
    reference_controller_recording, monkeypatch,
):
    """If a db operation inside the atomic block raises, every prior mutation rolls back.

    Setup: a recording run with one drained-eligible timing row.
    Inject: monkeypatch db.conn.execute to raise after the timing-row DELETE.
    Assert: capture_runs.draft is still 1, the timing row is still present,
    no attempts were inserted.
    """
    ctl = reference_controller_recording
    db = ctl.db
    run_id = ctl.recorder.capture_run_id
    assert run_id is not None

    # Seed one timing row that would be drained
    db.conn.execute(
        "INSERT INTO recorded_segment_times "
        "(capture_session_id, segment_id, time_ms, deaths, clean_tail_ms) "
        "VALUES (?, ?, ?, ?, ?)",
        (ctl.recorder.current_capture_session_id, "seg1", 1000, 0, 1000),
    )
    db.conn.commit()

    # Inject failure: wrap conn.execute so it raises on the INSERT into attempts
    original_execute = db.conn.execute
    call_count = {"n": 0}

    def failing_execute(sql, *args, **kwargs):
        call_count["n"] += 1
        if "INSERT INTO attempts" in sql:
            raise RuntimeError("injected failure mid-transaction")
        return original_execute(sql, *args, **kwargs)

    monkeypatch.setattr(db.conn, "execute", failing_execute)

    with pytest.raises(RuntimeError, match="injected failure"):
        await ctl.save_and_finish_run(Mode.REFERENCE, "Test Name")

    # Restore for assertions
    monkeypatch.undo()

    # capture_runs.draft still 1
    row = db.conn.execute(
        "SELECT draft, name FROM capture_runs WHERE id = ?", (run_id,),
    ).fetchone()
    assert row[0] == 1, "draft flag should have rolled back to 1"

    # Timing row still present
    rows = db.conn.execute(
        "SELECT id FROM recorded_segment_times "
        "WHERE capture_session_id = ?",
        (ctl.recorder.current_capture_session_id,),
    ).fetchall()
    assert len(rows) == 1, "drained timing row should have been restored"

    # No attempts inserted for this run
    rows = db.conn.execute(
        "SELECT id FROM attempts WHERE parent_id = ?", (run_id,),
    ).fetchall()
    assert len(rows) == 0, "no attempts should have been seeded after rollback"


@pytest.mark.asyncio
async def test_happy_path_commits_all_five_mutations(
    reference_controller_recording,
):
    """Happy path: capture session ended, timing rows drained, draft promoted,
    run activated, attempts seeded. Returns OK ActionResult."""
    ctl = reference_controller_recording
    db = ctl.db
    run_id = ctl.recorder.capture_run_id
    sess_id = ctl.recorder.current_capture_session_id
    assert run_id is not None
    assert sess_id is not None

    db.conn.execute(
        "INSERT INTO recorded_segment_times "
        "(capture_session_id, segment_id, time_ms, deaths, clean_tail_ms) "
        "VALUES (?, ?, ?, ?, ?)",
        (sess_id, "seg1", 1234, 2, 800),
    )
    db.conn.commit()

    result = await ctl.save_and_finish_run(Mode.REFERENCE, "Finalized Name")

    assert result.status == Status.OK
    assert result.new_mode == Mode.IDLE

    # capture_session ended
    sess_row = db.conn.execute(
        "SELECT ended_at FROM capture_sessions WHERE id = ?", (sess_id,),
    ).fetchone()
    assert sess_row[0] is not None

    # timing row drained
    rows = db.conn.execute(
        "SELECT id FROM recorded_segment_times WHERE capture_session_id = ?",
        (sess_id,),
    ).fetchall()
    assert rows == []

    # draft promoted + named
    cap = db.conn.execute(
        "SELECT draft, name, active FROM capture_runs WHERE id = ?", (run_id,),
    ).fetchone()
    assert cap[0] == 0
    assert cap[1] == "Finalized Name"
    assert cap[2] == 1

    # attempt seeded with REFERENCE source
    att = db.conn.execute(
        "SELECT segment_id, time_ms, deaths, source FROM attempts "
        "WHERE parent_id = ?", (run_id,),
    ).fetchone()
    assert att == ("seg1", 1234, 2, "reference")
```

- [ ] **Step 2: Create a `reference_controller_recording` fixture**

Append to `tests/unit/capture/conftest.py` (create the file if it doesn't exist — check first with `ls tests/unit/capture/conftest.py`):

```python
"""Fixtures for capture-package unit tests."""
from __future__ import annotations

from pathlib import Path

import pytest

from spinlab.capture.reference import ReferenceController
from spinlab.models import Mode


@pytest.fixture
def reference_controller_recording(tmp_path, monkeypatch):
    """A ReferenceController already in RECORDING state with a fresh capture_run + session.

    Uses an in-memory-backed Database (real sqlite) and a stub emu that records
    sent commands but is otherwise inert.
    """
    from spinlab.db import Database
    from tests.conftest import FakeEmuBackend  # the post-rename fake

    db_path = tmp_path / "test.db"
    db = Database(str(db_path))
    db.create_game("g1", "Test Game", rom_hash="abc")
    emu = FakeEmuBackend()
    emu._connected = True  # adjust attr name if needed — see tests/conftest.py

    ctl = ReferenceController(db, emu)
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    import asyncio
    asyncio.get_event_loop().run_until_complete(
        ctl.start_reference(Mode.IDLE, "g1", data_dir, run_name="In-Progress")
    )
    return ctl
```

Note: the exact `FakeEmuBackend` import path and `_connected` attribute name should match the post-rename layout from commit c84ef4c. Read `tests/conftest.py` to confirm. If `FakeEmuBackend.is_connected` is a `@property` that reads a private flag, set that flag directly.

- [ ] **Step 3: Run the tests to verify they pass against current code**

```
python -m pytest tests/unit/capture/test_finalizer.py -v
```

Expected: both tests PASS. They're documenting the current contract; if either fails, the production code has a bug that must be understood before extraction.

- [ ] **Step 4: Commit the tests as a pre-extraction safety net**

```
git add tests/unit/capture/test_finalizer.py tests/unit/capture/conftest.py
git commit -m "test(capture): document save_and_finish_run rollback contract pre-extraction"
```

### Task 1.2: Create the finalizer function

- [ ] **Step 1: Create `python/spinlab/capture/finalizer.py`**

```python
"""atomic_save_and_finish_run — atomic finalize of a recording capture_run.

Single-function module. The function is called from
ReferenceController.save_and_finish_run after the orchestrator confirms the
recording session has stopped. Five mutations happen inside one BEGIN
IMMEDIATE: end the capture session, drain recorded_segment_times for the
run, promote the draft to saved, activate this run (deactivating sibling
runs for the same game), and insert seeded Attempt rows from the drained
timing data.

The function is non-async: it operates on db.conn directly with explicit
transaction control. Caller is responsible for the recorder-state
transition to idle and any scheduler.rebuild_all_states() call — those are
not part of the atomic unit.
"""
from __future__ import annotations

import logging
from datetime import UTC
from datetime import datetime as _dt
from typing import TYPE_CHECKING

from spinlab.models import Attempt, AttemptSource

if TYPE_CHECKING:
    from spinlab.db import Database

logger = logging.getLogger(__name__)


def atomic_save_and_finish_run(
    db: "Database",
    run_id: str,
    session_id: str | None,
    name: str,
) -> list[Attempt]:
    """End session + drain timing rows + promote draft + activate + seed attempts.

    All five mutations happen inside a single BEGIN IMMEDIATE. Either every
    step succeeds and commits, or any failure rolls back and re-raises.

    Returns the seeded Attempt objects (empty list if there were no drained
    timing rows). Caller logs them.

    Raises whatever sqlite3 raises on a mid-transaction failure.
    """
    try:
        db.conn.execute("BEGIN IMMEDIATE")

        if session_id:
            db.conn.execute(
                "UPDATE capture_sessions SET ended_at = ?, end_reason = ? "
                "WHERE id = ? AND ended_at IS NULL",
                (_dt.now(UTC).isoformat(), "stopped", session_id),
            )

        rows = db.conn.execute(
            "SELECT t.id, t.capture_session_id, t.segment_id, t.time_ms, "
            "t.deaths, t.clean_tail_ms, t.recorded_at "
            "FROM recorded_segment_times t "
            "JOIN capture_sessions s ON t.capture_session_id = s.id "
            "WHERE s.capture_run_id = ? ORDER BY t.id",
            (run_id,),
        ).fetchall()
        timing_rows = [dict(r) for r in rows]
        ids = [r["id"] for r in timing_rows]
        if ids:
            placeholders = ",".join("?" * len(ids))
            db.conn.execute(
                f"DELETE FROM recorded_segment_times WHERE id IN ({placeholders})",
                ids,
            )

        db.conn.execute(
            "UPDATE capture_runs SET draft = 0, name = ? WHERE id = ?",
            (name, run_id),
        )

        game_row = db.conn.execute(
            "SELECT game_id FROM capture_runs WHERE id = ?", (run_id,),
        ).fetchone()
        if game_row:
            db.conn.execute(
                "UPDATE capture_runs SET active = 0 WHERE game_id = ?",
                (game_row[0],),
            )
            db.conn.execute(
                "UPDATE capture_runs SET active = 1 WHERE id = ?", (run_id,),
            )

        now = _dt.now(UTC)
        seeded: list[Attempt] = []
        for row in timing_rows:
            attempt = Attempt(
                segment_id=row["segment_id"],
                parent_id=run_id,
                completed=True,
                time_ms=row["time_ms"],
                deaths=row["deaths"],
                clean_tail_ms=row["clean_tail_ms"],
                source=AttemptSource.REFERENCE,
                created_at=now,
            )
            db.conn.execute(
                """INSERT INTO attempts
                   (segment_id, parent_id, completed, time_ms,
                    strat_version, source, deaths, clean_tail_ms,
                    observed_start_conditions, observed_end_conditions,
                    invalidated, chosen_allocator, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (attempt.segment_id, attempt.parent_id,
                 int(attempt.completed), attempt.time_ms,
                 attempt.strat_version, attempt.source,
                 attempt.deaths, attempt.clean_tail_ms,
                 attempt.observed_start_conditions,
                 attempt.observed_end_conditions,
                 int(attempt.invalidated),
                 attempt.chosen_allocator,
                 attempt.created_at.isoformat()),
            )
            seeded.append(attempt)

        db.conn.commit()
    except Exception:
        db.conn.rollback()
        raise

    return seeded
```

### Task 1.3: Re-point `save_and_finish_run` to the function

- [ ] **Step 1: Edit `python/spinlab/capture/reference.py`**

Find the `save_and_finish_run` method. Replace the `try: self.db.conn.execute("BEGIN IMMEDIATE") … except Exception: rollback; raise` block (currently lines 368-446 in the file as of 2026-05-11) with a call to the extracted function.

The new method body:

```python
    async def save_and_finish_run(
        self, mode: Mode, name: str, scheduler: "Scheduler | None" = None,
    ) -> ActionResult:
        """Combined Stop Session + Finalize, atomic.

        Two valid entry conditions:
          - mode == REFERENCE: full atomic stop + finalize.
          - mode == IDLE and paused_run_id is set: just finalize the paused
            run via the lighter `finalize_run` path.
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
        seeded = atomic_save_and_finish_run(self.db, run_id, sess_id, name)

        if scheduler:
            scheduler.rebuild_all_states()
        self._enter_idle()
        for attempt in seeded:
            logger.info("seed: segment=%s time=%dms deaths=%d clean_tail=%dms",
                         attempt.segment_id, attempt.time_ms, attempt.deaths,
                         attempt.clean_tail_ms)
        logger.info("reference: save_and_finish run=%s as %r (seeded %d attempts)",
                     run_id, name, len(seeded))
        return ActionResult(status=Status.OK, new_mode=Mode.IDLE)
```

- [ ] **Step 2: Run the unit tests**

```
python -m pytest tests/unit/capture/test_finalizer.py -v
```

Expected: both tests still PASS. Same behavior, new code path.

- [ ] **Step 3: Run the full suite**

```
python -m pytest
```

Expected: 804 passed (or baseline). All pre-existing tests of `save_and_finish_run` (including integration coverage from session manager tests) still green.

- [ ] **Step 4: Commit**

```
git add python/spinlab/capture/finalizer.py python/spinlab/capture/reference.py
git commit -m "refactor(capture): extract atomic_save_and_finish_run

100-line atomic SQL transaction lives in capture/finalizer.py as a
module-level function. ReferenceController.save_and_finish_run becomes
a ~15-line caller. Rollback contract preserved (verified by tests
landed in the pre-extraction commit)."
```

---

## Task 2 — Extract `FillGapController`

**Files:**
- Create: `python/spinlab/capture/fill_gap.py`
- Create: `tests/unit/capture/test_fill_gap.py`
- Modify: `python/spinlab/capture/reference.py` (drop fill_gap fields + methods)
- Modify: `python/spinlab/session_manager.py` (construct + dispatch through FillGapController)

### Task 2.1: Write FillGapController tests

- [ ] **Step 1: Create `tests/unit/capture/test_fill_gap.py`**

```python
"""Tests for FillGapController — extracted from ReferenceController."""
from __future__ import annotations

import pytest

from spinlab.capture.fill_gap import FillGapController
from spinlab.errors import (
    NoHotVariantError,
    NotConnectedError,
)
from spinlab.models import Mode, Status
from spinlab.protocol import FillGapLoadCmd, SpawnEvent


@pytest.fixture
def fg_db(tmp_path):
    """Database with one segment that has a hot waypoint save state."""
    from spinlab.db import Database
    db = Database(str(tmp_path / "fg.db"))
    db.create_game("g1", "Test", rom_hash="x")
    # Caller-specific setup: segments + hot save state.
    # The test author should consult tests/factories.py for the right helpers;
    # the production schema requires waypoints first, then segments referencing
    # them, then waypoint_save_states. See test_session_manager.py::TestFillGap
    # for the exact construction pattern.
    return db


@pytest.fixture
def fg_emu():
    """Stub emu that records sent commands."""
    from tests.conftest import FakeEmuBackend
    emu = FakeEmuBackend()
    emu._connected = True
    return emu


@pytest.mark.asyncio
async def test_start_raises_when_not_connected(fg_db, fg_emu):
    fg_emu._connected = False
    fg = FillGapController(fg_db, fg_emu)
    with pytest.raises(NotConnectedError):
        await fg.start("seg-nonexistent")


@pytest.mark.asyncio
async def test_start_raises_no_hot_variant(fg_db, fg_emu):
    """Segment exists but has no 'hot' save state on its start waypoint."""
    # Set up: one segment with a start_waypoint_id but no waypoint_save_state
    # with variant_type='hot'. See test_session_manager.py for construction.
    fg = FillGapController(fg_db, fg_emu)
    with pytest.raises(NoHotVariantError):
        await fg.start("seg-without-hot")


@pytest.mark.asyncio
async def test_start_happy_path(fg_db_with_hot, fg_emu):
    """start() sends FillGapLoadCmd and sets is_active."""
    fg = FillGapController(fg_db_with_hot, fg_emu)
    result = await fg.start("seg1")
    assert result.status == Status.STARTED
    assert result.new_mode == Mode.FILL_GAP
    assert fg.is_active is True
    assert fg.segment_id == "seg1"
    cmd = fg_emu.send_command.call_args[0][0]
    assert isinstance(cmd, FillGapLoadCmd)


def test_handle_spawn_returns_false_when_inactive(fg_db, fg_emu):
    fg = FillGapController(fg_db, fg_emu)
    assert fg.handle_spawn(SpawnEvent(state_path="/c.mss")) is False


def test_handle_spawn_returns_false_when_no_state_path(fg_db_with_hot, fg_emu):
    fg = FillGapController(fg_db_with_hot, fg_emu)
    fg._segment_id = "seg1"
    fg._waypoint_id = "wp1"
    assert fg.handle_spawn(SpawnEvent(state_path=None)) is False
    # State preserved — not yet consumed.
    assert fg.is_active is True


def test_handle_spawn_happy_path_persists_cold(fg_db_with_hot, fg_emu):
    """Happy path: cold state persisted on the start waypoint and state cleared."""
    fg = FillGapController(fg_db_with_hot, fg_emu)
    fg._segment_id = "seg1"
    fg._waypoint_id = "wp1"
    consumed = fg.handle_spawn(SpawnEvent(state_path="/cold1.mss"))
    assert consumed is True
    assert fg.is_active is False
    cold = fg_db_with_hot.get_save_state("wp1", "cold")
    assert cold is not None
    assert cold.state_path == "/cold1.mss"


def test_clear_resets_state(fg_db, fg_emu):
    fg = FillGapController(fg_db, fg_emu)
    fg._segment_id = "seg1"
    fg._waypoint_id = "wp1"
    fg.clear()
    assert fg.is_active is False
    assert fg.segment_id is None
```

The `fg_db_with_hot` fixture isn't defined above — add it to `tests/unit/capture/conftest.py` referencing the construction pattern in `tests/unit/test_session_manager.py::TestFillGap`.

- [ ] **Step 2: Run the tests to verify they fail with ImportError**

```
python -m pytest tests/unit/capture/test_fill_gap.py -v
```

Expected: collection FAIL — `from spinlab.capture.fill_gap import FillGapController` cannot resolve.

### Task 2.2: Create FillGapController

- [ ] **Step 1: Create `python/spinlab/capture/fill_gap.py`**

```python
"""FillGapController — captures the cold start state for a single segment.

Distinct from ColdFillController (which runs through a queue of segments
missing cold states). FillGap is single-shot: user picks one segment, hot
state loads, player dies, the next spawn captures the cold state, done.

State machine: IDLE → ACTIVE (after start()) → IDLE (after consuming a
SpawnEvent with a state_path).
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from spinlab.errors import NoHotVariantError, NotConnectedError
from spinlab.models import ActionResult, Mode, Status, WaypointSaveState
from spinlab.protocol import FillGapLoadCmd, SpawnEvent

if TYPE_CHECKING:
    from spinlab.db import Database
    from spinlab.emu_backend import EmuBackend

logger = logging.getLogger(__name__)


class FillGapController:
    def __init__(self, db: "Database", emu: "EmuBackend") -> None:
        self._db = db
        self._emu = emu
        self._segment_id: str | None = None
        self._waypoint_id: str | None = None

    @property
    def is_active(self) -> bool:
        return self._segment_id is not None

    @property
    def segment_id(self) -> str | None:
        return self._segment_id

    async def start(self, segment_id: str) -> ActionResult:
        if not self._emu.is_connected:
            raise NotConnectedError()
        row = self._db.conn.execute(
            "SELECT start_waypoint_id FROM segments WHERE id = ?", (segment_id,),
        ).fetchone()
        start_waypoint_id = row[0] if row else None
        hot = (self._db.get_save_state(start_waypoint_id, "hot")
               if start_waypoint_id else None)
        if not hot:
            raise NoHotVariantError()
        self._segment_id = segment_id
        self._waypoint_id = start_waypoint_id
        await self._emu.send_command(FillGapLoadCmd(
            state_path=hot.state_path,
            message="Die to capture cold start",
        ))
        return ActionResult(status=Status.STARTED, new_mode=Mode.FILL_GAP)

    def handle_spawn(self, event: SpawnEvent) -> bool:
        """Persist cold state if active and event carries a state_path.

        Returns True if the event was consumed (state persisted, controller
        returns to IDLE); False if not active or event has no state_path.
        """
        if not self.is_active or not event.state_path:
            return False
        if self._waypoint_id:
            self._db.add_save_state(WaypointSaveState(
                waypoint_id=self._waypoint_id,
                variant_type="cold",
                state_path=event.state_path,
                is_default=True,
            ))
        self.clear()
        return True

    def clear(self) -> None:
        self._segment_id = None
        self._waypoint_id = None
```

- [ ] **Step 2: Run the unit tests**

```
python -m pytest tests/unit/capture/test_fill_gap.py -v
```

Expected: all tests PASS.

### Task 2.3: Wire FillGapController into SessionManager

- [ ] **Step 1: Read `python/spinlab/session_manager.py` to find the fill_gap routing**

Look for:
- The `_handle_spawn` method (around line 286)
- The line `if self.capture.handle_fill_gap_spawn(event):`
- The `start_fill_gap` method (somewhere it calls `self.capture.start_fill_gap(...)`)

- [ ] **Step 2: Add `FillGapController` construction**

In `SessionManager.__init__`, alongside `self.cold_fill = ColdFillController(...)`, add:

```python
from spinlab.capture.fill_gap import FillGapController
self.fill_gap = FillGapController(self.db, self.emu)
```

- [ ] **Step 3: Re-point dispatch**

In `_handle_spawn`, replace:

```python
if self.capture.handle_fill_gap_spawn(event):
    return
```

with:

```python
if self.fill_gap.handle_spawn(event):
    return
```

In the existing `start_fill_gap` method (if it lives on SessionManager and currently calls `self.capture.start_fill_gap(...)`), redirect to `self.fill_gap.start(...)`.

- [ ] **Step 4: Remove the migrated code from `capture/reference.py`**

Delete these from `ReferenceController`:

- The two fields:
  ```python
  self.fill_gap_segment_id: str | None = None
  self._fill_gap_waypoint_id: str | None = None
  ```
- The `start_fill_gap` method (currently lines 551-565).
- The `handle_fill_gap_spawn` method (currently lines 567-581).

Remove the now-unused imports from `reference.py`:
- `NoHotVariantError` from `..errors` (verify no other use in the file)
- `FillGapLoadCmd` from `..protocol`

- [ ] **Step 5: Run the full suite**

```
python -m pytest
```

Expected: 804 passed. Pay particular attention to:
- `tests/unit/test_session_manager.py::TestFillGap`
- Any other test that calls `controller.handle_fill_gap_spawn` or `controller.start_fill_gap` directly. Update those test imports to use the new controller.

- [ ] **Step 6: Commit**

```
git add python/spinlab/capture/fill_gap.py tests/unit/capture/test_fill_gap.py \
        python/spinlab/capture/reference.py python/spinlab/session_manager.py
git commit -m "refactor(capture): extract FillGapController

ReferenceController no longer owns fill-gap state. SessionManager grows
self.fill_gap alongside self.cold_fill. Dispatch unchanged from the
outside; \`_handle_spawn\` now routes through fill_gap.handle_spawn
instead of capture.handle_fill_gap_spawn."
```

---

## Task 3 — Extract `MovieController`

**Files:**
- Create: `python/spinlab/retroarch/movies.py`
- Create: `tests/unit/retroarch/test_movies.py`
- Modify: `python/spinlab/retroarch/orchestrator.py`

### Task 3.1: Write MovieController tests

- [ ] **Step 1: Create `tests/unit/retroarch/test_movies.py`**

```python
"""Tests for MovieController — owns RA movie record/playback lifecycle."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from spinlab.errors import BackendNotImplementedError
from spinlab.protocol import (
    SPEED_UNCAPPED,
    ReplayErrorEvent,
    ReplayFinishedEvent,
    ReplayStartedEvent,
)
from spinlab.retroarch.movies import MovieController
from spinlab.retroarch.raclient import (
    MoviePlaybackError,
    RAClientError,
)


@pytest.fixture
def raclient_mock():
    m = MagicMock()
    m.record_movie = AsyncMock()
    m.play_movie = AsyncMock()
    m.fast_forward_toggle = MagicMock()
    return m


@pytest.fixture
def emitted_events():
    events: list[object] = []
    return events


@pytest.fixture
def mc(raclient_mock, emitted_events):
    return MovieController(
        raclient=raclient_mock,
        enable=True,
        on_event=emitted_events.append,
    )


@pytest.mark.asyncio
async def test_start_recording_noop_when_disabled(raclient_mock, emitted_events):
    mc = MovieController(raclient_mock, enable=False, on_event=emitted_events.append)
    await mc.start_recording(Path("/tmp/x.replay"))
    raclient_mock.record_movie.assert_not_called()


@pytest.mark.asyncio
async def test_start_recording_nonfatal_on_raclient_error(mc, raclient_mock):
    raclient_mock.record_movie.side_effect = RAClientError("boom")
    # Must not raise
    await mc.start_recording(Path("/tmp/x.replay"))
    assert mc.is_recording is False


@pytest.mark.asyncio
async def test_stop_recording_noop_when_nothing_active(mc, raclient_mock):
    # No prior start_recording call
    await mc.stop_recording()
    # No exception, no calls on a recording handle (there isn't one)


@pytest.mark.asyncio
async def test_start_playback_raises_when_disabled(raclient_mock, emitted_events):
    mc = MovieController(raclient_mock, enable=False, on_event=emitted_events.append)
    with pytest.raises(BackendNotImplementedError):
        await mc.start_playback(Path("/tmp/x.replay"), speed=0)


@pytest.mark.asyncio
async def test_start_playback_emits_started(mc, raclient_mock, emitted_events):
    playback = MagicMock()
    playback.frame_count = 1234
    playback.stop = AsyncMock()
    raclient_mock.play_movie.return_value = playback

    await mc.start_playback(Path("/x.replay"), speed=0)

    assert len(emitted_events) == 1
    ev = emitted_events[0]
    assert isinstance(ev, ReplayStartedEvent)
    assert ev.frame_count == 1234


@pytest.mark.asyncio
async def test_start_playback_uncapped_toggles_fast_forward(
    mc, raclient_mock, emitted_events,
):
    playback = MagicMock()
    playback.frame_count = 100
    playback.stop = AsyncMock()
    raclient_mock.play_movie.return_value = playback

    await mc.start_playback(Path("/x.replay"), speed=SPEED_UNCAPPED)

    raclient_mock.fast_forward_toggle.assert_called_once()


@pytest.mark.asyncio
async def test_start_playback_capped_does_not_toggle(
    mc, raclient_mock, emitted_events,
):
    playback = MagicMock()
    playback.frame_count = 100
    playback.stop = AsyncMock()
    raclient_mock.play_movie.return_value = playback

    await mc.start_playback(Path("/x.replay"), speed=0)

    raclient_mock.fast_forward_toggle.assert_not_called()


@pytest.mark.asyncio
async def test_start_playback_movie_error_emits_replay_error(
    mc, raclient_mock, emitted_events,
):
    raclient_mock.play_movie.side_effect = MoviePlaybackError("verify failed")
    await mc.start_playback(Path("/x.replay"), speed=0)
    assert len(emitted_events) == 1
    assert isinstance(emitted_events[0], ReplayErrorEvent)


@pytest.mark.asyncio
async def test_stop_playback_symmetric_toggle(
    mc, raclient_mock, emitted_events,
):
    playback = MagicMock()
    playback.frame_count = 100
    playback.stop = AsyncMock()
    raclient_mock.play_movie.return_value = playback

    await mc.start_playback(Path("/x.replay"), speed=SPEED_UNCAPPED)
    raclient_mock.fast_forward_toggle.reset_mock()
    await mc.stop_playback()

    raclient_mock.fast_forward_toggle.assert_called_once()
    assert any(isinstance(e, ReplayFinishedEvent) for e in emitted_events)


@pytest.mark.asyncio
async def test_stop_playback_no_toggle_when_not_fast_forwarding(
    mc, raclient_mock, emitted_events,
):
    playback = MagicMock()
    playback.frame_count = 100
    playback.stop = AsyncMock()
    raclient_mock.play_movie.return_value = playback

    await mc.start_playback(Path("/x.replay"), speed=0)
    raclient_mock.fast_forward_toggle.reset_mock()
    await mc.stop_playback()

    raclient_mock.fast_forward_toggle.assert_not_called()


@pytest.mark.asyncio
async def test_stop_playback_idempotent(mc, raclient_mock):
    # Second call should be a no-op
    await mc.stop_playback()
    await mc.stop_playback()
```

- [ ] **Step 2: Run to verify they fail with ImportError**

```
python -m pytest tests/unit/retroarch/test_movies.py -v
```

Expected: collection FAIL.

### Task 3.2: Create MovieController

- [ ] **Step 1: Create `python/spinlab/retroarch/movies.py`**

```python
"""MovieController — owns RA movie record/playback lifecycle.

Extracted from RetroArchOrchestrator. Holds the cross-call state
(_active_recording, _active_playback, _fast_forwarding) that the four
movie command handlers previously kept inline on the orchestrator.

The fast-forward toggle requires symmetric pairing: NCI's FAST_FORWARD is
a flip with no state query, so any code path that toggles ON must toggle
OFF in the stop handler. That contract is enforced here.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path

from spinlab.errors import BackendNotImplementedError
from spinlab.protocol import (
    SPEED_UNCAPPED,
    ReplayErrorEvent,
    ReplayFinishedEvent,
    ReplayStartedEvent,
)
from spinlab.retroarch.raclient import (
    MoviePlayback,
    MoviePlaybackError,
    MovieRecording,
    RAClient,
    RAClientError,
)

logger = logging.getLogger(__name__)


class MovieController:
    def __init__(
        self,
        raclient: RAClient,
        enable: bool,
        on_event: Callable[[object], None],
    ) -> None:
        self._raclient = raclient
        self._enable = enable
        self._on_event = on_event
        self._active_recording: MovieRecording | None = None
        self._active_playback: MoviePlayback | None = None
        self._fast_forwarding: bool = False

    @property
    def is_recording(self) -> bool:
        return self._active_recording is not None

    @property
    def is_playing(self) -> bool:
        return self._active_playback is not None

    async def start_recording(self, path: Path) -> None:
        """Start movie recording. Non-fatal on RAClientError."""
        if not self._enable:
            logger.info("Reference recording started (movies disabled)")
            return
        try:
            self._active_recording = await self._raclient.record_movie(path)
            logger.info("Movie recording started: %s", path)
        except RAClientError as exc:
            logger.warning("Movie recording failed to start: %s", exc)

    async def stop_recording(self) -> None:
        """Stop movie recording if active. Non-fatal on RAClientError."""
        if self._active_recording is None:
            logger.info("Reference recording stopped (no movie recorder active)")
            return
        try:
            stopped_path = await self._active_recording.stop()
            logger.info("Movie recording stopped: %s", stopped_path)
        except RAClientError as exc:
            logger.warning("Movie recording failed to stop: %s", exc)
        finally:
            self._active_recording = None

    async def start_playback(self, path: Path, speed: int) -> None:
        """Start movie playback. Raises if movies disabled.

        Emits ReplayStartedEvent on success, ReplayErrorEvent on RAClient
        failure. Does NOT raise on RAClient errors — the caller (command
        dispatcher) shouldn't propagate them as exceptions.

        If speed == SPEED_UNCAPPED, toggles RA into fast-forward; the
        matching toggle-off happens in stop_playback().
        """
        if not self._enable:
            logger.warning("MovieController: start_playback rejected — movies disabled")
            raise BackendNotImplementedError()

        try:
            self._active_playback = await self._raclient.play_movie(path)
        except MoviePlaybackError as exc:
            logger.error("Movie replay verification failed: %s", exc)
            self._on_event(ReplayErrorEvent(message=str(exc)))
            return
        except RAClientError as exc:
            logger.error("Movie replay failed: %s", exc)
            self._on_event(ReplayErrorEvent(message=str(exc)))
            return

        if speed == SPEED_UNCAPPED:
            await asyncio.to_thread(self._raclient.fast_forward_toggle)
            self._fast_forwarding = True

        self._on_event(ReplayStartedEvent(
            path=str(path),
            frame_count=self._active_playback.frame_count,
        ))
        logger.info(
            "Movie replay started: %s (frames=%d, fast_forward=%s)",
            path, self._active_playback.frame_count, self._fast_forwarding,
        )

    async def stop_playback(self) -> None:
        """Stop playback. Idempotent — second call is a no-op.

        Symmetric fast-forward toggle: if start_playback toggled ON, this
        toggles OFF. Emits ReplayFinishedEvent.
        """
        if self._active_playback is None:
            return
        try:
            await self._active_playback.stop()
        except RAClientError as exc:
            logger.warning("Movie replay failed to stop: %s", exc)
        finally:
            self._active_playback = None
            if self._fast_forwarding:
                await asyncio.to_thread(self._raclient.fast_forward_toggle)
                self._fast_forwarding = False
        self._on_event(ReplayFinishedEvent())
        logger.info("Movie replay stopped")
```

- [ ] **Step 2: Run the unit tests**

```
python -m pytest tests/unit/retroarch/test_movies.py -v
```

Expected: all 11 tests PASS.

### Task 3.3: Wire MovieController into orchestrator

- [ ] **Step 1: Edit `python/spinlab/retroarch/orchestrator.py`**

In `RetroArchOrchestrator.__init__`:

Remove the three fields:
```python
self._active_recording: MovieRecording | None = None
self._active_playback: MoviePlayback | None = None
self._fast_forwarding: bool = False
```

Replace the `enable_movies` constructor parameter handling with a `movies: MovieController` parameter:

```python
def __init__(
    self,
    *,
    raclient: RAClient,
    poller,
    conditions: ConditionRegistry,
    practice_timing: PracticeTiming,
    speed_run_timing: SpeedRunTiming,
    state_paths: StatePathResolver,
    movies: MovieController,
) -> None:
    self._raclient = raclient
    self._poller = poller
    self._conditions = conditions
    self._practice_timing = practice_timing
    self._speed_run_timing = speed_run_timing
    self._state_paths = state_paths
    self._movies = movies
    # ... rest unchanged
```

Replace the four movie command handlers:

```python
async def _on_reference_start(self, cmd: ReferenceStartCmd) -> None:
    await self._movies.start_recording(Path(cmd.path))

async def _on_reference_stop(self, cmd: ReferenceStopCmd) -> None:
    await self._movies.stop_recording()

async def _on_replay(self, cmd: ReplayCmd) -> None:
    await self._movies.start_playback(Path(cmd.path), cmd.speed)

async def _on_replay_stop(self, cmd: ReplayStopCmd) -> None:
    await self._movies.stop_playback()
```

Remove the now-unused imports:
- `MoviePlayback`, `MoviePlaybackError`, `MovieRecording` from `spinlab.retroarch.raclient`
- `BackendNotImplementedError` from `spinlab.errors` (it lived inline in `_on_replay`)
- `ReplayErrorEvent`, `ReplayFinishedEvent`, `ReplayStartedEvent` from `spinlab.protocol`
- `SPEED_UNCAPPED` from `spinlab.protocol`

Add the new import:
```python
from spinlab.retroarch.movies import MovieController
```

Note that `on_poller_event` previously delegated to `self.events.put_nowait(ev)` directly. The movie events that were emitted via `self.on_poller_event(...)` inside the old `_on_replay` / `_on_replay_stop` paths now go through `MovieController.on_event`, which is the same callable. Pass `self.on_poller_event` (or, more precisely, `lambda ev: self.on_poller_event(ev)`) into the `MovieController` constructor in `build_orchestrator`. See Task 3.4.

- [ ] **Step 2: Update `build_orchestrator` to construct MovieController**

Still in `orchestrator.py` (the factory is moved out in Task 4 — for now it stays here):

```python
# Inside build_orchestrator, after raclient is built:
from spinlab.retroarch.movies import MovieController

# (existing code creates raclient, conditions, timing, state_paths, poller …)

# Construct movies AFTER orch exists (it needs orch.on_poller_event as callback).
# So build orch first with a placeholder, then bind.
orch = RetroArchOrchestrator(
    raclient=raclient,
    poller=poller,
    conditions=conditions,
    practice_timing=practice_timing,
    speed_run_timing=speed_run_timing,
    state_paths=state_paths,
    movies=...,  # see below
)
```

This is awkward because `MovieController` needs `orch.on_poller_event` but `orch` needs `movies`. Resolve by constructing `MovieController` BEFORE `orch` with a placeholder callback, then re-binding:

```python
movies = MovieController(
    raclient=raclient,
    enable=(movie_dir is not None),
    on_event=lambda ev: None,  # rebound below
)
orch = RetroArchOrchestrator(
    raclient=raclient, poller=poller, conditions=conditions,
    practice_timing=practice_timing, speed_run_timing=speed_run_timing,
    state_paths=state_paths, movies=movies,
)
movies._on_event = orch.on_poller_event  # rebind
deps.on_event = orch.on_poller_event
return orch
```

If the `_on_event` private rebind feels too hacky, expose a setter on `MovieController`:

```python
# In MovieController:
def set_event_callback(self, on_event: Callable[[object], None]) -> None:
    self._on_event = on_event
```

…and call `movies.set_event_callback(orch.on_poller_event)`. Pick one style and stick with it.

- [ ] **Step 3: Update orchestrator tests**

Search `tests/unit/retroarch/test_orchestrator.py` for `enable_movies` usages. Replace with the `movies=MovieController(...)` construction pattern. The test helper `_build_orchestrator` likely needs to grow a `movies` parameter that defaults to a `MovieController` over a mock raclient.

- [ ] **Step 4: Run the full suite**

```
python -m pytest
```

Expected: 804 passed. Movies-related test files:
- `tests/unit/retroarch/test_movies.py` (new) — green
- `tests/unit/retroarch/test_orchestrator.py` — green (with updated construction)
- `tests/integration/test_replay_fixture.py` — green (uses the full wiring)

- [ ] **Step 5: Commit**

```
git add python/spinlab/retroarch/movies.py tests/unit/retroarch/test_movies.py \
        python/spinlab/retroarch/orchestrator.py \
        tests/unit/retroarch/test_orchestrator.py
git commit -m "refactor(retroarch): extract MovieController

Movie record/playback state and the fast-forward symmetric toggle
contract now live in retroarch/movies.py. The four movie command
handlers on RetroArchOrchestrator become 1-line delegations. New
unit tests cover the fast-forward toggle symmetry and error
emission, both of which were previously only exercised by the
replay-fixture integration test."
```

---

## Task 4 — Move `build_orchestrator` to `wiring.py`

**Files:**
- Create: `python/spinlab/retroarch/wiring.py`
- Modify: `python/spinlab/retroarch/orchestrator.py`
- Modify: `python/spinlab/dashboard.py`
- Modify: `tests/unit/retroarch/test_orchestrator.py` (or wherever `build_orchestrator` is imported in tests)

This task is a pure mechanical move. No test changes beyond import paths.

- [ ] **Step 1: Create `python/spinlab/retroarch/wiring.py`**

Cut the `build_orchestrator` function from `orchestrator.py` (currently lines 381-475 as of 2026-05-11) and paste it into a new `wiring.py`. Add the necessary imports at the top:

```python
"""build_orchestrator — config-driven construction of a RetroArchOrchestrator.

Separated from orchestrator.py so that file is purely about implementing
the EmuBackend protocol. This module handles AppConfig parsing, path
resolution (movie dir, RA log dir), and dependency-injection wiring.
"""
from __future__ import annotations

import logging
from pathlib import Path

from spinlab.condition_registry import ConditionRegistry
from spinlab.retroarch.movies import MovieController
from spinlab.retroarch.orchestrator import RetroArchOrchestrator
from spinlab.retroarch.raclient import RAClient
from spinlab.state_paths import StatePathResolver
from spinlab.timing import PracticeTiming, SpeedRunTiming

logger = logging.getLogger(__name__)


def build_orchestrator(config) -> RetroArchOrchestrator:
    # ... full function body from orchestrator.py
```

- [ ] **Step 2: Remove `build_orchestrator` from `orchestrator.py`**

Delete the function. Remove now-unused imports from `orchestrator.py`:
- `StatePathResolver` (only used by the factory)
- Any `Path`-related parsing imports if they're now unused

Verify by running:

```
python -c "from spinlab.retroarch.orchestrator import RetroArchOrchestrator"
```

Should succeed with no errors.

- [ ] **Step 3: Update `dashboard.py` import**

Find the line `from spinlab.retroarch.orchestrator import build_orchestrator` and replace it with `from spinlab.retroarch.wiring import build_orchestrator`.

- [ ] **Step 4: Update test imports**

```
grep -rn "from spinlab.retroarch.orchestrator import build_orchestrator" tests/
grep -rn "spinlab.retroarch.orchestrator.build_orchestrator" tests/
```

For every match, change the import path to `spinlab.retroarch.wiring`.

- [ ] **Step 5: Run the full suite**

```
python -m pytest
```

Expected: 804 passed.

- [ ] **Step 6: Commit**

```
git add python/spinlab/retroarch/wiring.py python/spinlab/retroarch/orchestrator.py \
        python/spinlab/dashboard.py tests/unit/retroarch/
git commit -m "refactor(retroarch): move build_orchestrator to wiring.py

Pure mechanical relocation. orchestrator.py is now purely about
implementing the EmuBackend protocol; wiring.py owns config parsing,
path resolution, and dependency-injection construction. No behavior
change; only consumer imports updated (dashboard + tests)."
```

---

## Final verification

- [ ] **Step 1: Confirm file sizes match targets**

```
wc -l python/spinlab/capture/reference.py python/spinlab/retroarch/orchestrator.py
wc -l python/spinlab/capture/{finalizer,fill_gap}.py python/spinlab/retroarch/{movies,wiring}.py
```

Expected:
- `reference.py` ≈ 520 lines (down from 660)
- `orchestrator.py` ≈ 310 lines (down from 475)
- Each new file under 120 lines

- [ ] **Step 2: Full pytest one more time**

```
python -m pytest
```

Expected: 804 passed, ~50s.

- [ ] **Step 3: Update memory**

Add a new memory file at `C:\Users\thedo\.claude\projects\c--Users-thedo-git-spinlab\memory\project_encapsulation_pass_2026_05_11.md`:

```markdown
---
name: Encapsulation pass — capture/ + retroarch/ — DONE 2026-05-11
description: Four extractions landed; reference.py 660→~520 lines; orchestrator.py 475→~310 lines; raclient.py split deferred until tests land.
type: project
---
Encapsulation pass shipped 2026-05-11 across four commits. Spec at
`docs/superpowers/specs/2026-05-11-capture-retroarch-encapsulation-design.md`;
plan at `docs/superpowers/plans/2026-05-11-capture-retroarch-encapsulation.md`.

New files: `capture/finalizer.py`, `capture/fill_gap.py`,
`retroarch/movies.py`, `retroarch/wiring.py`. Each has its own focused
unit test. ~20 new unit tests total.

**How to apply:** If asked "where did X go?" — fill-gap state and methods
are now `FillGapController` (constructed by SessionManager alongside
ColdFillController); movie record/playback state is `MovieController`
(owned by RetroArchOrchestrator); atomic finalize SQL is the
`atomic_save_and_finish_run` function in `capture/finalizer.py`;
`build_orchestrator` lives in `retroarch/wiring.py`.

**Deferred:** raclient.py (761 lines) split is sequenced after RAClient
unit tests land (separate plan).
```

Add to `MEMORY.md`:

```markdown
- [project_encapsulation_pass_2026_05_11.md](project_encapsulation_pass_2026_05_11.md) — Encapsulation pass complete: FillGapController, MovieController, atomic finalizer extracted; build_orchestrator moved to wiring.py.
```

---

## Risks and mitigations

- **MovieController callback rebinding.** The orchestrator and MovieController are mutually dependent: MovieController emits events through `orch.on_poller_event`, but `orch` needs `movies` to construct. Mitigation in Task 3.3 is a setter (`set_event_callback`) or attribute rebinding after both objects exist. Pick one and document it.

- **FillGapController test fixture complexity.** The `fg_db_with_hot` fixture needs a fully-constructed game/level/segment/waypoint/save-state graph. The construction pattern is established in `tests/unit/test_session_manager.py::TestFillGap`; copy that exact pattern into the new conftest rather than improvising. If improvising produces a schema-mismatch error, that's the signal to copy harder.

- **Finalizer rollback test injects via monkeypatch.** The injected `failing_execute` wraps `db.conn.execute`. Make sure it forwards `*args, **kwargs` correctly — sqlite's `execute()` is positional-only for the SQL but the parameter binding can be positional or keyword. The provided test code uses positional only, matching production usage.

- **Task ordering matters.** Task 1 (finalizer) must precede Task 2 (fill_gap) because the test fixtures for fill_gap depend on a clean `ReferenceController` state, and the finalizer extraction makes `save_and_finish_run` shorter and easier to reason about when reviewing the fill_gap diff.
