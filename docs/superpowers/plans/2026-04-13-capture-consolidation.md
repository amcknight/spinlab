# Capture Consolidation + Seeding Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Consolidate the five capture-adjacent modules into a `spinlab.capture/` package with sharper names, then diagnose and fix the reference-seeding bug so reference run times actually land as first-attempt practice data. This is Phase 2 of the 2026-04-13 cleanup pass ([spec](../specs/2026-04-13-cleanup-pass-design.md), sections 1–2, sequencing step 2).

**Architecture:** Move each module with `git mv` to preserve blame, apply mechanical class/field renames (`ReferenceCapture`→`SegmentRecorder`, `RefSegmentTime`→`RecordedSegmentTime`, `CaptureController`→`ReferenceController`), fold `reference_seeding.py` into the new `capture/draft.py` as a private helper, update all importers (`session_manager.py`, routes, tests). Then add a regression test for the seeding bug, run it, let the failure point at H1 (missing timestamps from Lua → empty `segment_times`) vs H2 (scheduler=None at save site), and fix the proven hypothesis.

**Tech Stack:** Python 3.11, pytest, git mv. No DB schema changes, no TCP protocol changes, no Lua changes *unless* H1 is diagnosed.

---

## Constraints

- **Use `git mv`** for every file relocation — blame preservation matters; plain `mv` + `git add` loses history.
- **One commit per task.** Keeps reverts surgical.
- **Run `pytest -m "not (emulator or slow or frontend)"` after every task.** Collection errors from stale imports are the most likely regression.
- **Public method signatures on orchestrators are preserved.** Only names and import paths change. If a signature needs to change to fix seeding, that belongs to the seeding-fix tasks (7–9), not the rename tasks.
- **`__init__.py` re-exports** `SegmentRecorder`, `DraftManager`, `ReferenceController`, `ColdFillController`, `RecordedSegmentTime`. External callers import from `spinlab.capture`; internal code inside `spinlab/capture/` uses relative imports.
- **No false unification.** `SessionManager` keeps `self.capture` (reference) and `self.cold_fill` as separate attributes. Do not collapse them.
- **Do not delete `reference_seeding.py`'s behavior** — only its file. The `seed_reference_attempts` function becomes a module-private `_seed_reference_attempts` inside `capture/draft.py`.

## File layout (target)

```
python/spinlab/capture/
  __init__.py          # re-exports public types
  recorder.py          # was reference_capture.py    — SegmentRecorder + RecordedSegmentTime
  draft.py             # was draft_manager.py + reference_seeding.py folded in
  reference.py         # was capture_controller.py   — ReferenceController
  cold_fill.py         # was cold_fill_controller.py — ColdFillController (unchanged)
```

Deleted at the end of the rename pass:

- `python/spinlab/reference_capture.py`
- `python/spinlab/draft_manager.py`
- `python/spinlab/reference_seeding.py`
- `python/spinlab/capture_controller.py`
- `python/spinlab/cold_fill_controller.py`

## Rename table

| Old symbol | New symbol |
|-----|-----|
| `ReferenceCapture` (class) | `SegmentRecorder` |
| `RefSegmentTime` (dataclass) | `RecordedSegmentTime` |
| `CaptureController` (class) | `ReferenceController` |
| `seed_reference_attempts` (function) | `_seed_reference_attempts` (private, inside `draft.py`) |
| `ColdFillController` | unchanged |
| `DraftManager` | unchanged |

On attribute names held by other classes:

- `SessionManager.capture` (holds `ReferenceController`) — unchanged attribute name, but rebind to the renamed class.
- `ReferenceController.ref_capture` (holds `SegmentRecorder`) — rename to `ReferenceController.recorder` for consistency.

---

## Task 1: Create `capture/` package skeleton

**Files:**
- Create: `python/spinlab/capture/__init__.py`

- [ ] **Step 1: Create the package directory with an empty `__init__.py`**

```python
# python/spinlab/capture/__init__.py
"""Capture pipeline: segment recording, draft lifecycle, reference/replay/cold-fill orchestration."""
```

- [ ] **Step 2: Verify collection**

Run: `pytest -m "not (emulator or slow or frontend)" --collect-only -q | tail -5`
Expected: no errors; same collection count as before.

- [ ] **Step 3: Commit**

```bash
git add python/spinlab/capture/__init__.py
git commit -m "chore: create empty spinlab.capture package"
```

---

## Task 2: Move `reference_capture.py` → `capture/recorder.py` with renames

**Files:**
- Move: `python/spinlab/reference_capture.py` → `python/spinlab/capture/recorder.py`
- Modify: `python/spinlab/capture/recorder.py` (rename classes + imports)
- Modify: `python/spinlab/capture/__init__.py` (re-export)

- [ ] **Step 1: Move the file**

```bash
git mv python/spinlab/reference_capture.py python/spinlab/capture/recorder.py
```

- [ ] **Step 2: Rename `ReferenceCapture` → `SegmentRecorder` and `RefSegmentTime` → `RecordedSegmentTime` inside `recorder.py`**

Edit `python/spinlab/capture/recorder.py`:

- Replace the module docstring `"""ReferenceCapture — owns ..."""` with `"""SegmentRecorder — owns reference/replay segment capture state and logic."""`.
- `@dataclass class RefSegmentTime:` → `@dataclass class RecordedSegmentTime:` (and update its docstring `"""Timing data for one segment captured during a reference run."""` — leave the text, just the class name changes).
- `class ReferenceCapture:` → `class SegmentRecorder:` (docstring stays: `"""Captures segments during reference runs and replays."""`).
- In `_close_segment` (around line 127), `self.segment_times.append(RefSegmentTime(...))` → `self.segment_times.append(RecordedSegmentTime(...))`.

No other edits — logic is identical.

- [ ] **Step 3: Re-export from `capture/__init__.py`**

```python
# python/spinlab/capture/__init__.py
"""Capture pipeline: segment recording, draft lifecycle, reference/replay/cold-fill orchestration."""
from .recorder import SegmentRecorder, RecordedSegmentTime

__all__ = ["SegmentRecorder", "RecordedSegmentTime"]
```

- [ ] **Step 4: Fix the one remaining internal importer so the suite still collects**

`python/spinlab/draft_manager.py` has `from .reference_capture import RefSegmentTime` under `TYPE_CHECKING`. Update to:

```python
if TYPE_CHECKING:
    from .db import Database
    from .capture.recorder import RecordedSegmentTime
    from .scheduler import Scheduler
```

And update its one usage at line 33:

```python
segment_times: "list[RecordedSegmentTime] | None" = None,
```

`python/spinlab/reference_seeding.py` also references `RefSegmentTime` under TYPE_CHECKING (line 12):

```python
if TYPE_CHECKING:
    from .db import Database
    from .capture.recorder import RecordedSegmentTime
```

And its one usage at line 20: `segment_times: list["RecordedSegmentTime"],`.

`python/spinlab/capture_controller.py` at line 20: `from .reference_capture import ReferenceCapture` → `from .capture.recorder import SegmentRecorder`. Then in the class body:

- Line 37: `self.ref_capture = ReferenceCapture()` → `self.recorder = SegmentRecorder()`.
- All subsequent `self.ref_capture.*` → `self.recorder.*` inside this file (11 occurrences in `capture_controller.py`).

- [ ] **Step 5: Run fast tests**

Run: `pytest -m "not (emulator or slow or frontend)" -x`
Expected: PASS.

If you see `ImportError: cannot import name 'ReferenceCapture'` or `'RefSegmentTime'` from a test file, that's expected — the next task handles test file renames. If the error is from `python/spinlab/` code, you missed an importer; search with `grep -rn "ReferenceCapture\|RefSegmentTime" python/spinlab/` and fix.

Actually — test files `tests/unit/capture/test_reference_capture.py`, `test_reference_seeding.py`, `test_capture_controller.py`, `test_capture_with_conditions.py`, and `test_cold_fill.py` import these symbols. Update them in-place now (don't rename the test files yet — Task 6 does that):

Search: `grep -rn "from spinlab.reference_capture\|import ReferenceCapture\|import RefSegmentTime" tests/`

For each hit, replace:
- `from spinlab.reference_capture import ReferenceCapture` → `from spinlab.capture import SegmentRecorder`
- `from spinlab.reference_capture import RefSegmentTime` → `from spinlab.capture import RecordedSegmentTime`
- `from spinlab.reference_capture import ReferenceCapture, RefSegmentTime` → `from spinlab.capture import SegmentRecorder, RecordedSegmentTime`
- `ReferenceCapture(` → `SegmentRecorder(`
- `RefSegmentTime(` → `RecordedSegmentTime(`

Then rerun: `pytest -m "not (emulator or slow or frontend)" -x`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "refactor: move reference_capture → capture.recorder, rename ReferenceCapture → SegmentRecorder"
```

---

## Task 3: Fold `reference_seeding.py` into `capture/draft.py`

**Files:**
- Move: `python/spinlab/draft_manager.py` → `python/spinlab/capture/draft.py`
- Delete: `python/spinlab/reference_seeding.py` (content folded into `draft.py`)
- Modify: `python/spinlab/capture/draft.py`
- Modify: `python/spinlab/capture/__init__.py`

- [ ] **Step 1: Move draft_manager**

```bash
git mv python/spinlab/draft_manager.py python/spinlab/capture/draft.py
```

- [ ] **Step 2: Fold `seed_reference_attempts` into `draft.py` as a module-private helper**

Edit `python/spinlab/capture/draft.py` to this content (replacing the whole file):

```python
"""DraftManager — owns draft reference lifecycle state and seeds reference attempts on save."""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ..models import ActionResult, Attempt, AttemptSource, Status

if TYPE_CHECKING:
    from ..db import Database
    from ..scheduler import Scheduler
    from .recorder import RecordedSegmentTime

logger = logging.getLogger(__name__)


def _seed_reference_attempts(
    db: "Database",
    capture_run_id: str,
    segment_times: list["RecordedSegmentTime"],
) -> int:
    """Insert seed attempts from reference segment times. Returns count inserted."""
    if not segment_times:
        return 0

    now = datetime.now(UTC)
    count = 0
    for rst in segment_times:
        attempt = Attempt(
            segment_id=rst.segment_id,
            session_id=capture_run_id,
            completed=True,
            time_ms=rst.time_ms,
            deaths=rst.deaths,
            clean_tail_ms=rst.clean_tail_ms,
            source=AttemptSource.REFERENCE,
            created_at=now,
        )
        db.log_attempt(attempt)
        count += 1
        logger.info("seed: segment=%s time=%dms deaths=%d clean_tail=%dms",
                     rst.segment_id, rst.time_ms, rst.deaths, rst.clean_tail_ms)

    return count


class DraftManager:
    """Manages draft capture runs (pending save/discard after recording or replay)."""

    def __init__(self) -> None:
        self.run_id: str | None = None
        self.segments_count: int = 0

    @property
    def has_draft(self) -> bool:
        return self.run_id is not None

    def enter_draft(self, run_id: str | None, segments_count: int) -> None:
        """Populate draft state from a completed capture/replay."""
        self.run_id = run_id
        self.segments_count = segments_count

    def save(
        self, db: "Database", name: str,
        segment_times: "list[RecordedSegmentTime] | None" = None,
        scheduler: "Scheduler | None" = None,
    ) -> ActionResult:
        """Promote draft capture run to saved reference, seed attempts, rebuild model."""
        if not self.run_id:
            return ActionResult(status=Status.NO_DRAFT)
        db.promote_draft(self.run_id, name)
        db.set_active_capture_run(self.run_id)

        if segment_times:
            _seed_reference_attempts(db, self.run_id, segment_times)
            if scheduler:
                scheduler.rebuild_all_states()

        self.run_id = None
        self.segments_count = 0
        return ActionResult(status=Status.OK)

    def discard(self, db: "Database") -> ActionResult:
        """Hard-delete draft capture run and all associated data."""
        if not self.run_id:
            return ActionResult(status=Status.NO_DRAFT)
        db.hard_delete_capture_run(self.run_id)
        self.run_id = None
        self.segments_count = 0
        return ActionResult(status=Status.OK)

    def recover(self, db: "Database", game_id: str) -> None:
        """On startup, check for orphaned draft capture runs and restore state."""
        rows = db.conn.execute(
            "SELECT id FROM capture_runs WHERE game_id = ? AND draft = 1 ORDER BY created_at DESC",
            (game_id,),
        ).fetchall()
        if not rows:
            return
        self.run_id = rows[0][0]
        self.segments_count = db.conn.execute(
            "SELECT COUNT(*) FROM segments WHERE reference_id = ? AND active = 1",
            (self.run_id,),
        ).fetchone()[0]
        for row in rows[1:]:
            db.hard_delete_capture_run(row[0])

    def get_state(self) -> dict | None:
        """Return draft dict for get_state() or None."""
        if not self.run_id:
            return None
        return {
            "run_id": self.run_id,
            "segments_captured": self.segments_count,
        }
```

Note the changes vs the original `draft_manager.py`:
- `from .models` → `from ..models` (we're one level deeper).
- `from .reference_capture import RefSegmentTime` → `from .recorder import RecordedSegmentTime`.
- `from .reference_seeding import seed_reference_attempts` (the inline import) is gone — call `_seed_reference_attempts` directly.
- `Attempt, AttemptSource` now imported at module top (no longer needed by a separate file).

- [ ] **Step 3: Delete the now-empty `reference_seeding.py`**

```bash
git rm python/spinlab/reference_seeding.py
```

- [ ] **Step 4: Re-export `DraftManager` from `capture/__init__.py`**

Edit `python/spinlab/capture/__init__.py`:

```python
"""Capture pipeline: segment recording, draft lifecycle, reference/replay/cold-fill orchestration."""
from .draft import DraftManager
from .recorder import SegmentRecorder, RecordedSegmentTime

__all__ = ["DraftManager", "SegmentRecorder", "RecordedSegmentTime"]
```

- [ ] **Step 5: Fix the one remaining internal importer**

`python/spinlab/capture_controller.py` line 21: `from .draft_manager import DraftManager` → `from .capture.draft import DraftManager`.

- [ ] **Step 6: Fix tests that import these symbols**

Search: `grep -rn "from spinlab.draft_manager\|from spinlab.reference_seeding" tests/`

For each hit:
- `from spinlab.draft_manager import DraftManager` → `from spinlab.capture import DraftManager`.
- `from spinlab.reference_seeding import seed_reference_attempts` → `from spinlab.capture.draft import _seed_reference_attempts as seed_reference_attempts` (preserves the call sites; tests can be cleaned up later when they move in Task 6).

- [ ] **Step 7: Run fast tests**

Run: `pytest -m "not (emulator or slow or frontend)" -x`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "refactor: fold reference_seeding into capture.draft, move draft_manager → capture.draft"
```

---

## Task 4: Move `capture_controller.py` → `capture/reference.py` with rename

**Files:**
- Move: `python/spinlab/capture_controller.py` → `python/spinlab/capture/reference.py`
- Modify: `python/spinlab/capture/reference.py`
- Modify: `python/spinlab/capture/__init__.py`
- Modify: `python/spinlab/session_manager.py`
- Modify: `python/spinlab/routes/reference.py` (if it imports `CaptureController` directly)

- [ ] **Step 1: Move the file**

```bash
git mv python/spinlab/capture_controller.py python/spinlab/capture/reference.py
```

- [ ] **Step 2: Update imports inside `capture/reference.py`**

At the top of the file, the import block currently reads:

```python
from .models import ActionResult, Mode, Status
from .protocol import (
    SPEED_UNCAPPED,
    ReferenceStartCmd, ReferenceStopCmd, ReplayCmd, ReplayStopCmd,
    FillGapLoadCmd,
)
from .capture.recorder import SegmentRecorder
from .capture.draft import DraftManager
from .condition_registry import ConditionRegistry
```

(Note: `.capture.recorder` / `.capture.draft` were set in Tasks 2–3; they were written as relative imports from the old flat location. Now that this file is inside `spinlab/capture/`, rewrite them all.)

Replace with:

```python
from ..models import ActionResult, Mode, Status
from ..protocol import (
    SPEED_UNCAPPED,
    ReferenceStartCmd, ReferenceStopCmd, ReplayCmd, ReplayStopCmd,
    FillGapLoadCmd,
)
from .recorder import SegmentRecorder
from .draft import DraftManager
from ..condition_registry import ConditionRegistry
```

And the `TYPE_CHECKING` block:

```python
if TYPE_CHECKING:
    from ..db import Database
    from ..tcp_manager import TcpManager
```

Also update the inline `from .models import WaypointSaveState` inside `handle_fill_gap_spawn` (around line 178) to `from ..models import WaypointSaveState`.

- [ ] **Step 3: Rename `CaptureController` → `ReferenceController`**

In `capture/reference.py`:
- `class CaptureController:` → `class ReferenceController:`
- Module docstring: `"""CaptureController — orchestrates reference recording and replay capture.` → `"""ReferenceController — orchestrates reference recording and replay capture.`

- [ ] **Step 4: Re-export from `capture/__init__.py`**

```python
"""Capture pipeline: segment recording, draft lifecycle, reference/replay/cold-fill orchestration."""
from .draft import DraftManager
from .recorder import SegmentRecorder, RecordedSegmentTime
from .reference import ReferenceController

__all__ = ["DraftManager", "ReferenceController", "SegmentRecorder", "RecordedSegmentTime"]
```

- [ ] **Step 5: Update `session_manager.py`**

At `python/spinlab/session_manager.py:23`:
`from .capture_controller import CaptureController` → `from .capture import ReferenceController`.

At `python/spinlab/session_manager.py:74`:
`self.capture = CaptureController(db, tcp)` → `self.capture = ReferenceController(db, tcp)`.

(The attribute name `self.capture` stays — only the class reference changes.)

- [ ] **Step 6: Update any test imports**

Search: `grep -rn "from spinlab.capture_controller\|import CaptureController" tests/ python/spinlab/`

For each hit in `tests/`:
- `from spinlab.capture_controller import CaptureController` → `from spinlab.capture import ReferenceController`
- `CaptureController(` → `ReferenceController(`

If any `python/spinlab/` files outside what's listed in steps 2–5 still reference `CaptureController`, stop and investigate — all production call sites should now be accounted for.

- [ ] **Step 7: Run fast tests**

Run: `pytest -m "not (emulator or slow or frontend)" -x`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "refactor: move capture_controller → capture.reference, rename CaptureController → ReferenceController"
```

---

## Task 5: Move `cold_fill_controller.py` → `capture/cold_fill.py`

**Files:**
- Move: `python/spinlab/cold_fill_controller.py` → `python/spinlab/capture/cold_fill.py`
- Modify: `python/spinlab/capture/cold_fill.py` (relative import adjustments only)
- Modify: `python/spinlab/capture/__init__.py`
- Modify: `python/spinlab/session_manager.py`

- [ ] **Step 1: Move the file**

```bash
git mv python/spinlab/cold_fill_controller.py python/spinlab/capture/cold_fill.py
```

- [ ] **Step 2: Fix relative imports inside `capture/cold_fill.py`**

Find each `from .` import at the top of the file and change it to `from ..` (since we dropped one level deeper). Run `grep -n "^from \." python/spinlab/capture/cold_fill.py` to enumerate them, then prefix each with an extra dot.

For any inline imports inside function/method bodies (`from .models import ...`), also update to `from ..models import ...`.

Class name `ColdFillController` is unchanged.

- [ ] **Step 3: Re-export**

`python/spinlab/capture/__init__.py`:

```python
"""Capture pipeline: segment recording, draft lifecycle, reference/replay/cold-fill orchestration."""
from .cold_fill import ColdFillController
from .draft import DraftManager
from .recorder import SegmentRecorder, RecordedSegmentTime
from .reference import ReferenceController

__all__ = [
    "ColdFillController",
    "DraftManager",
    "ReferenceController",
    "SegmentRecorder",
    "RecordedSegmentTime",
]
```

- [ ] **Step 4: Update `session_manager.py`**

Search: `grep -n "cold_fill_controller\|ColdFillController" python/spinlab/session_manager.py`

Replace `from .cold_fill_controller import ColdFillController` → `from .capture import ColdFillController`. The `self.cold_fill = ColdFillController(...)` construction line is unchanged.

- [ ] **Step 5: Update test imports**

Search: `grep -rn "from spinlab.cold_fill_controller" tests/`
Replace `from spinlab.cold_fill_controller import ColdFillController` → `from spinlab.capture import ColdFillController`.

- [ ] **Step 6: Run full fast suite**

Run: `pytest -m "not (emulator or slow or frontend)" -x`
Expected: PASS.

Also verify `python -c "from spinlab.capture import SegmentRecorder, DraftManager, ReferenceController, ColdFillController, RecordedSegmentTime; print('ok')"` prints `ok`.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "refactor: move cold_fill_controller → capture.cold_fill"
```

---

## Task 6: Rename capture tests to match new module layout

**Files:**
- Rename: `tests/unit/capture/test_reference_capture.py` → `test_recorder.py`
- Rename: `tests/unit/capture/test_capture_controller.py` → `test_reference.py`
- Merge: `tests/unit/capture/test_reference_seeding.py` + `test_draft_lifecycle.py` → `test_draft.py`
- Keep: `tests/unit/capture/test_cold_fill.py`, `test_cold_fill_integration.py`, `test_capture_with_conditions.py` (names unchanged — they already read sensibly)

- [ ] **Step 1: Rename recorder test**

```bash
git mv tests/unit/capture/test_reference_capture.py tests/unit/capture/test_recorder.py
```

- [ ] **Step 2: Rename reference controller test**

```bash
git mv tests/unit/capture/test_capture_controller.py tests/unit/capture/test_reference.py
```

- [ ] **Step 3: Merge seeding + draft tests into `test_draft.py`**

```bash
git mv tests/unit/capture/test_draft_lifecycle.py tests/unit/capture/test_draft.py
```

Then append the contents of `test_reference_seeding.py` into `test_draft.py` (excluding the `import` block — deduplicate with what's already at the top of `test_draft.py`). Delete the seeding file:

```bash
git rm tests/unit/capture/test_reference_seeding.py
```

Manually inspect `tests/unit/capture/test_draft.py` and consolidate imports at the top so there's one clean import block (no duplicate `from spinlab.capture import ...` lines).

- [ ] **Step 4: Run full fast suite**

Run: `pytest -m "not (emulator or slow or frontend)" -x`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "test: rename capture tests to match new module layout; merge seeding into test_draft"
```

---

## Task 7: Write the regression test for seeding

**Files:**
- Modify: `tests/unit/capture/test_draft.py`

The regression test drives a `SegmentRecorder` through fake events (entrance → exit with timestamps → `_close_segment` produces `RecordedSegmentTime`), passes those recorded times into `DraftManager.save()` with a mock scheduler, and asserts:

1. `attempts` table has rows with `source='reference'` matching the recorded times.
2. `scheduler.rebuild_all_states()` was called.

- [ ] **Step 1: Add the failing test**

Append to `tests/unit/capture/test_draft.py`:

```python
from unittest.mock import MagicMock

from spinlab.capture import DraftManager, SegmentRecorder
from spinlab.condition_registry import ConditionRegistry
from spinlab.models import AttemptSource


def test_save_draft_seeds_attempts_and_rebuilds_model(db_with_game):
    """Reference run segment times become first-attempt practice data after save.

    Regression test for the seeding bug diagnosed in the 2026-04-13 cleanup pass spec
    (section 2). The recorder must produce RecordedSegmentTime entries from entrance+exit
    events with timestamps, and DraftManager.save must both seed them as attempts AND
    rebuild scheduler state so estimators see the data.
    """
    db, game_id = db_with_game

    recorder = SegmentRecorder()
    registry = ConditionRegistry()

    run_id = "live_regression_test"
    db.create_capture_run(run_id, game_id, "Regression", draft=True)
    recorder.capture_run_id = run_id

    # Drive: entrance at t=0, exit at t=12345 (goal reached)
    recorder.handle_entrance({
        "level": 1,
        "state_path": "/tmp/entrance.state",
        "timestamp_ms": 0,
        "conditions": {},
    })
    recorder.handle_exit(
        {"level": 1, "goal": "exit", "timestamp_ms": 12345, "conditions": {}},
        game_id=game_id, db=db, registry=registry,
    )

    assert len(recorder.segment_times) == 1, (
        "recorder must produce a RecordedSegmentTime from entrance+exit with timestamps"
    )
    assert recorder.segment_times[0].time_ms == 12345

    draft = DraftManager()
    draft.enter_draft(run_id, recorder.segments_count)

    scheduler = MagicMock()
    result = draft.save(
        db, name="Regression run",
        segment_times=recorder.segment_times,
        scheduler=scheduler,
    )

    assert result.status.name == "OK"
    rows = db.conn.execute(
        "SELECT time_ms, source FROM attempts WHERE session_id = ?", (run_id,)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == 12345
    assert rows[0][1] == AttemptSource.REFERENCE.value
    scheduler.rebuild_all_states.assert_called_once()
```

The test relies on a `db_with_game` fixture. If it doesn't exist in `tests/unit/capture/conftest.py` or `tests/factories.py` already, add it to `tests/unit/capture/conftest.py`:

```python
# tests/unit/capture/conftest.py
import pytest
from tests.factories import make_db, make_game  # existing helpers

@pytest.fixture
def db_with_game(tmp_path):
    db = make_db(tmp_path)
    game = make_game(db)
    return db, game.id
```

(If `make_db` / `make_game` aren't named exactly that, inspect `tests/factories.py` and `tests/conftest.py` to find the right helpers — do not invent fixtures that don't exist.)

- [ ] **Step 2: Run the new test — it should FAIL**

Run: `pytest tests/unit/capture/test_draft.py::test_save_draft_seeds_attempts_and_rebuilds_model -v`

Expected failure mode tells us the hypothesis:

- If the first assert `len(recorder.segment_times) == 1` fails → **H1 confirmed**: the recorder isn't producing timing data despite timestamps being passed in. Move to Task 8.
- If that assert passes but `rows` is empty or `scheduler.rebuild_all_states.assert_called_once()` fails → **H2 confirmed**: save path has a wiring hole. Move to Task 8.
- If the test PASSES unexpectedly → the bug manifests only at a higher integration layer (likely H2 via routes). Extend the test with an extra case that calls `SessionManager.save_draft` instead of `DraftManager.save` directly; that path is where `scheduler=None` may creep in. See `python/spinlab/session_manager.py:382` — it calls `_get_scheduler()` which is conditional on `self.game_id`. If `game_id` is None at save time, scheduler is None silently.

Write down the failure mode observed — it determines Task 8's fix.

- [ ] **Step 3: Do NOT commit yet**

The red test is a commit-worthy artifact, but commit it together with the fix in Task 8 so bisect lands on a complete green.

---

## Task 8: Diagnose and fix the seeding bug

This task has two branches. Execute only the branch that Task 7's failure mode confirmed.

### Branch A — H1 (recorder produces no segment times)

Re-read [capture/recorder.py](../../python/spinlab/capture/recorder.py):

- `_close_segment` at the old line 117–132 only appends to `segment_times` when both `start_ts is not None and end_timestamp_ms is not None`.
- `handle_entrance` (old line 60–67) reads `event.get("timestamp_ms", 0)`. If Lua sends `level_entrance` without a `timestamp_ms` field, `start.get("timestamp_ms")` returns `0`, which is `not None` — so start is fine.
- `handle_exit` (old line 174–189) passes `end_timestamp_ms=event.get("timestamp_ms")` which is `None` if Lua omits the field.

Check `python/spinlab/protocol.py` around line 64–69:

```python
@dataclass
class LevelExitEvent:
    event: str = "level_exit"
    level: int = 0
    goal: str = "abort"
    conditions: dict = field(default_factory=dict)
```

Note: **no `timestamp_ms` field.** Same for `LevelEntranceEvent` (line 34–39). Checkpoints and spawns have `timestamp_ms`. Exits do not.

- [ ] **Step A1: Add `timestamp_ms` to `LevelEntranceEvent` and `LevelExitEvent`**

Edit `python/spinlab/protocol.py`:

```python
@dataclass
class LevelEntranceEvent:
    event: str = "level_entrance"
    level: int = 0
    state_path: str | None = None
    timestamp_ms: int = 0
    conditions: dict = field(default_factory=dict)

@dataclass
class LevelExitEvent:
    event: str = "level_exit"
    level: int = 0
    goal: str = "abort"
    timestamp_ms: int = 0
    conditions: dict = field(default_factory=dict)
```

- [ ] **Step A2: Update Lua to emit `timestamp_ms` on these events**

Search: `grep -n "level_entrance\|level_exit" lua/*.lua`

For each emission site of `level_entrance` / `level_exit`, include `timestamp_ms = <current ms>` in the payload using the same helper already used by the checkpoint event (search checkpoint emit for the helper name). Do not invent a new timing source.

- [ ] **Step A3: Update the test to assert Lua emission path**

Leave the unit test from Task 7 as the protocol-level assertion. Add a Lua-side assertion only if `tests/unit/test_protocol.py` has a contract test that validates event field shapes — if so, extend it to cover the new field. If not, skip (emulator tests will catch Lua regressions).

- [ ] **Step A4: Rerun the regression test**

Run: `pytest tests/unit/capture/test_draft.py::test_save_draft_seeds_attempts_and_rebuilds_model -v`
Expected: PASS.

### Branch B — H2 (scheduler is None at save site)

- [ ] **Step B1: Trace the call chain**

Start at `python/spinlab/routes/reference.py:70`: `return _check_result(await session.save_draft(name))`. Then `session_manager.py:382–390`:

```python
async def save_draft(self, name: str) -> ActionResult:
    scheduler = self._get_scheduler() if self.game_id else None
    result = await self.capture.save_draft(name, scheduler=scheduler)
```

If `self.game_id` is None at this moment, scheduler is silently None, seeding runs, but `rebuild_all_states()` doesn't fire. Practical question: when is `self.game_id` None at save time? Likely when game detection ran during the reference session but state was cleared between stop and save.

- [ ] **Step B2: Fix by sourcing `game_id` from the draft, not live session state**

The draft's `run_id` maps to a `capture_runs` row with `game_id`. Add a lookup in `SessionManager.save_draft`:

```python
async def save_draft(self, name: str) -> ActionResult:
    game_id = self.game_id
    if game_id is None and self.capture.draft.run_id:
        row = self.db.conn.execute(
            "SELECT game_id FROM capture_runs WHERE id = ?",
            (self.capture.draft.run_id,),
        ).fetchone()
        if row:
            game_id = row[0]
    scheduler = self._get_scheduler_for(game_id) if game_id else None
    result = await self.capture.save_draft(name, scheduler=scheduler)
    ...
```

If `_get_scheduler` only works for `self.game_id`, add a `_get_scheduler_for(game_id)` variant that accepts an explicit id. Check the existing `_get_scheduler` signature before adding — it may already accept an argument.

- [ ] **Step B3: Add a test that covers the session-level path**

Extend `test_save_draft_seeds_attempts_and_rebuilds_model` with a sibling test that constructs a `SessionManager` with `self.game_id = None` but a draft for the same game, calls `session.save_draft(name)`, and asserts attempts are seeded + scheduler rebuild fired. Use existing `SessionManager` test fixtures from `tests/unit/test_session_manager.py` as a template (do not recreate).

- [ ] **Step B4: Rerun both tests**

Run: `pytest tests/unit/capture/test_draft.py -v`
Expected: both tests PASS.

### Commit (both branches)

- [ ] **Step 9: Full fast suite + commit**

Run: `pytest -m "not (emulator or slow or frontend)" -x`
Expected: PASS.

```bash
git add -A
git commit -m "fix: seed reference segment times as first-attempt practice data

Adds regression test that drives SegmentRecorder through entrance+exit
events and asserts DraftManager.save seeds attempts and triggers
scheduler rebuild. Fixes [H1 / H2 — fill in which]."
```

---

## Task 9: Full verification

- [ ] **Step 1: Full pytest (all markers)**

Run: `pytest`
Expected: PASS. Per the project rule, a red suite is never acceptable — fix every failure, even pre-existing ones not caused by this work.

- [ ] **Step 2: Frontend**

Run: `cd frontend && npm run build && npm test && npm run typecheck`
Expected: PASS on all three.

- [ ] **Step 3: Manual smoke**

Boot `spinlab dashboard`. Load a ROM. Record a short reference run (two levels). Save the draft. Check the DB:

```bash
spinlab db query "SELECT COUNT(*) FROM attempts WHERE source = 'reference'"
```

Expected: count > 0, matching the number of segments recorded.

If the CLI subcommand `db query` doesn't exist, open the DB file directly with `sqlite3` and run the query manually.

- [ ] **Step 4: Final commit (if any cleanup was needed)**

If Steps 1–3 surfaced anything, fix and commit with a descriptive message. Otherwise no commit needed.

---

## Follow-ups (out of scope for this plan)

- **Status enum pruning** (spec section 6) — now easier because route error paths are consolidated by the rename. Next plan.
- **Frontend Playwright smoke** (section 3) — independent; next or parallel.
- **`model.ts` split, Overlay timer removal, dead code audit, test pruning** — later phases in the cleanup pass.
