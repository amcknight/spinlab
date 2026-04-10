# Test Quality Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a dedicated coverage visibility tool and write gap-filling tests for the five modules where incidental coverage is masking real testing gaps.

**Architecture:** One standalone Python script (`scripts/dedicated_coverage.py`) runs the fast test suite with coverage contexts and reports per-module dedicated vs suite-wide coverage. Five test tasks then plug the biggest gaps using real in-memory databases + fake TCP managers (not mocks of mocks).

**Tech Stack:** Python 3.11+, pytest, pytest-cov (already installed, v7.1.0), coverage.py (v7.13.5, supports `--cov-context=test`), dataclasses for models.

**Spec:** See [2026-04-08-test-quality-improvements-design.md](../specs/2026-04-08-test-quality-improvements-design.md)

---

## Baseline

Before starting: run the full test suite to confirm no pre-existing failures.

```bash
cd c:/Users/thedo/git/spinlab && python -m pytest
```

Expected: all tests pass. If any fail, fix them before starting this plan (per the `feedback_fix_preexisting_failures` user instruction).

---

## Task 1: Enable coverage contexts in pyproject.toml

**Files:**
- Modify: `pyproject.toml`

Enabling `--cov-context=test` at the plugin level adds per-test attribution to `.coverage` with negligible overhead (~0.5s on the 6s fast suite). This is the foundation for everything else.

- [ ] **Step 1: Add dynamic_context to `[tool.coverage.run]`**

Edit `pyproject.toml`. Locate:

```toml
[tool.coverage.run]
source = ["spinlab"]
data_file = "coverage/.coverage"
```

Add `dynamic_context = "test_function"`:

```toml
[tool.coverage.run]
source = ["spinlab"]
data_file = "coverage/.coverage"
dynamic_context = "test_function"
```

- [ ] **Step 2: Verify contexts are populated**

Run:

```bash
cd c:/Users/thedo/git/spinlab && python -m pytest tests/test_session_manager.py --cov=spinlab.session_manager --cov-context=test -q
```

Then query the SQLite DB to confirm contexts exist:

```bash
python -c "
import sqlite3
conn = sqlite3.connect('coverage/.coverage')
contexts = conn.execute(\"SELECT DISTINCT context FROM context WHERE context LIKE '%test_session_manager%' LIMIT 3\").fetchall()
print(f'Contexts found: {len(contexts)}')
for c in contexts:
    print(c[0])
conn.close()
"
```

Expected: at least one `tests/test_session_manager.py::...` context printed.

- [ ] **Step 3: Commit**

```bash
cd c:/Users/thedo/git/spinlab && git add pyproject.toml && git commit -m "test: enable per-test coverage contexts

Adds dynamic_context='test_function' so the .coverage SQLite DB tags
each line with the test that covered it. Enables the upcoming
dedicated_coverage.py script to separate intentional coverage from
incidental drive-by coverage."
```

---

## Task 2: Write dedicated_coverage.py script

**Files:**
- Create: `scripts/dedicated_coverage.py`
- Test: manual — run against current suite

The script runs fast tests with contexts, then for each `python/spinlab/**/*.py` module:
1. Checks whether `tests/test_<module_basename>.py` exists
2. If yes, queries `.coverage` DB for (line count covered by contexts starting with `tests/test_<module>.py::`) vs (line count covered by any context)
3. Computes dedicated % and suite-wide % against module's total statement count
4. Prints a sorted table

**Dedication rule:** Test file is `tests/test_<basename>.py` where `<basename>` is the module filename without `.py`. Example: `python/spinlab/session_manager.py` → `tests/test_session_manager.py`. No mapping dict.

- [ ] **Step 1: Create the script with the top-level structure**

Create `scripts/dedicated_coverage.py`:

```python
#!/usr/bin/env python
"""Dedicated coverage report: per-module honest vs. suite-wide coverage.

Runs the fast test suite with per-test coverage contexts, then for each
spinlab module reports:
  - Dedicated %: coverage from tests in tests/test_<module>.py only
  - Suite-wide %: coverage from the entire fast suite
  - Gap: suite-wide minus dedicated (larger = more incidental coverage)

A "dedicated" test file is identified purely by convention:
  python/spinlab/foo.py  →  tests/test_foo.py

No mapping table. If tests/test_<module>.py does not exist, dedicated
coverage is reported as 0% (no dedicated tests).

Usage:
  python scripts/dedicated_coverage.py

Fast tests only. Emulator/integration tests are inherently cross-cutting
and including them would make "incidental" a misleading label.
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
COVERAGE_DB = REPO_ROOT / "coverage" / ".coverage"
SPINLAB_DIR = REPO_ROOT / "python" / "spinlab"
TESTS_DIR = REPO_ROOT / "tests"


def run_fast_tests_with_contexts() -> None:
    """Run the fast test suite with per-test coverage contexts."""
    cmd = [
        sys.executable, "-m", "pytest", "tests/",
        "--ignore=tests/integration",
        "--ignore=tests/playwright",
        "-m", "not (emulator or slow or frontend)",
        "--cov=spinlab",
        "--cov-context=test",
        "--cov-report=",  # suppress terminal report, we'll build our own
        "-q",
    ]
    print("Running fast suite with per-test coverage contexts...")
    result = subprocess.run(cmd, cwd=REPO_ROOT)
    if result.returncode != 0:
        print(f"pytest exited with code {result.returncode}", file=sys.stderr)
        sys.exit(result.returncode)


def spinlab_modules() -> list[Path]:
    """Return all non-empty spinlab .py files, sorted."""
    modules = []
    for p in sorted(SPINLAB_DIR.rglob("*.py")):
        if p.name == "__init__.py":
            continue
        if p.stat().st_size == 0:
            continue
        modules.append(p)
    return modules


def dedicated_test_file(module: Path) -> Path | None:
    """Return tests/test_<basename>.py if it exists, else None."""
    candidate = TESTS_DIR / f"test_{module.stem}.py"
    return candidate if candidate.exists() else None


def module_coverage(conn: sqlite3.Connection, module: Path) -> tuple[int, int, int]:
    """Return (total_stmts, covered_suite_wide, covered_dedicated) for a module.

    "Covered" is the number of distinct executable line numbers that any
    context hit. Dedicated counts only contexts from tests/test_<module>.py.
    total_stmts is the module's statement count from coverage's file table.
    """
    # coverage.py stores file paths with forward slashes in context DB on
    # Windows too, but the relative path format varies. Match by trailing
    # path for robustness.
    rel = str(module.relative_to(REPO_ROOT)).replace("\\", "/")
    basename = module.name

    file_row = conn.execute(
        "SELECT id FROM file WHERE path LIKE ?",
        (f"%{basename}",),
    ).fetchone()
    if file_row is None:
        return (0, 0, 0)
    file_id = file_row[0]

    # Suite-wide: distinct lines covered by any context
    # line_bits stores a bitmap per (file_id, context_id); we need to OR them.
    rows = conn.execute(
        "SELECT numbits FROM line_bits WHERE file_id = ?",
        (file_id,),
    ).fetchall()
    suite_lines: set[int] = set()
    for (numbits,) in rows:
        suite_lines |= _numbits_to_lines(numbits)

    # Dedicated: only contexts from tests/test_<module>.py
    test_file = dedicated_test_file(module)
    dedicated_lines: set[int] = set()
    if test_file is not None:
        prefix = f"tests/test_{module.stem}.py::"
        rows = conn.execute(
            """
            SELECT lb.numbits
            FROM line_bits lb
            JOIN context c ON lb.context_id = c.id
            WHERE lb.file_id = ?
              AND c.context LIKE ?
            """,
            (file_id, f"{prefix}%"),
        ).fetchall()
        for (numbits,) in rows:
            dedicated_lines |= _numbits_to_lines(numbits)

    # Total statements: read the file and count executable lines.
    # Coverage stores this in a separate way; we ask coverage via its API.
    total = _statement_count(module)

    return (total, len(suite_lines), len(dedicated_lines))


def _numbits_to_lines(numbits: bytes) -> set[int]:
    """Decode coverage.py's numbits blob into a set of line numbers."""
    from coverage.numbits import numbits_to_nums
    return set(numbits_to_nums(numbits))


def _statement_count(module: Path) -> int:
    """Return the number of executable statements in a module via coverage.py."""
    from coverage import Coverage
    cov = Coverage(data_file=str(COVERAGE_DB))
    cov.load()
    analysis = cov.analysis2(str(module))
    # analysis2 returns (filename, executable_lines, excluded_lines,
    #                    missing_lines, missing_formatted)
    executable_lines = analysis[1]
    return len(executable_lines)


def format_pct(covered: int, total: int) -> str:
    if total == 0:
        return "  n/a"
    return f"{100 * covered / total:5.0f}%"


def main() -> int:
    run_fast_tests_with_contexts()

    if not COVERAGE_DB.exists():
        print(f"ERROR: {COVERAGE_DB} not found after pytest run", file=sys.stderr)
        return 1

    conn = sqlite3.connect(COVERAGE_DB)
    rows = []
    for module in spinlab_modules():
        total, suite, dedicated = module_coverage(conn, module)
        if total == 0:
            continue
        has_tests = dedicated_test_file(module) is not None
        rows.append((module, total, suite, dedicated, has_tests))
    conn.close()

    # Sort by gap, descending (biggest gaps first)
    def gap_pct(r):
        _, total, suite, dedicated, _ = r
        if total == 0:
            return 0
        return (suite - dedicated) / total
    rows.sort(key=gap_pct, reverse=True)

    print()
    print(f"{'Module':<32}{'Dedicated':>12}{'Suite-wide':>12}{'Gap':>8}  Has test file")
    print("-" * 78)
    for module, total, suite, dedicated, has_tests in rows:
        name = str(module.relative_to(SPINLAB_DIR)).replace("\\", "/").removesuffix(".py")
        ded_str = format_pct(dedicated, total)
        suite_str = format_pct(suite, total)
        if total > 0:
            gap_str = f"{100 * (suite - dedicated) / total:5.0f}%"
        else:
            gap_str = "  n/a"
        marker = "yes" if has_tests else "no"
        print(f"{name:<32}{ded_str:>12}{suite_str:>12}{gap_str:>8}  {marker}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Run the script and verify output**

Run:

```bash
cd c:/Users/thedo/git/spinlab && python scripts/dedicated_coverage.py
```

Expected output: a table showing modules sorted by gap descending. `capture_controller` should show ~0% dedicated and ~90% suite-wide (huge gap). `session_manager` should show ~61% dedicated and ~77% suite-wide. Health check: modules like `protocol` and `models` should have small or zero gaps.

If the script errors, debug. Common issues:
- Path mismatches in the SQLite query: print `conn.execute("SELECT path FROM file").fetchall()` to see actual stored paths
- Missing `coverage.numbits` module: it's part of coverage.py 7.x, should be available

- [ ] **Step 3: Commit**

```bash
cd c:/Users/thedo/git/spinlab && git add scripts/dedicated_coverage.py && git commit -m "test: add dedicated coverage reporting script

scripts/dedicated_coverage.py runs the fast suite with per-test
contexts, then reports per-module dedicated vs suite-wide coverage.
Convention: tests/test_<module>.py is the dedicated test file.
No mapping config; renames break visibly instead of silently.

Surfaces incidental coverage that hides real testing gaps — e.g.,
capture_controller shows 92% suite-wide but 0% dedicated."
```

---

## Task 3: Fake TcpManager test helper

**Files:**
- Modify: `tests/conftest.py`

Tasks 4 and 5 both need a fake TcpManager that records sent commands and can simulate connection state. Build it once in `conftest.py` as a reusable fixture to avoid duplication.

- [ ] **Step 1: Write the failing test**

Create a new file `tests/test_fake_tcp.py`:

```python
"""Meta-test: verify the fake_tcp fixture behavior."""
import pytest

from spinlab.protocol import ReferenceStartCmd


@pytest.mark.asyncio
async def test_fake_tcp_records_commands(fake_tcp):
    await fake_tcp.send_command(ReferenceStartCmd(path="/tmp/foo.spinrec"))
    assert len(fake_tcp.sent_commands) == 1
    assert isinstance(fake_tcp.sent_commands[0], ReferenceStartCmd)
    assert fake_tcp.sent_commands[0].path == "/tmp/foo.spinrec"


@pytest.mark.asyncio
async def test_fake_tcp_is_connected_default(fake_tcp):
    assert fake_tcp.is_connected is True


@pytest.mark.asyncio
async def test_fake_tcp_can_simulate_disconnected(fake_tcp):
    fake_tcp.is_connected = False
    assert fake_tcp.is_connected is False
```

- [ ] **Step 2: Run the test to verify it fails**

Run:

```bash
cd c:/Users/thedo/git/spinlab && python -m pytest tests/test_fake_tcp.py -v
```

Expected: FAIL with "fixture 'fake_tcp' not found".

- [ ] **Step 3: Add the fixture to conftest.py**

Edit `tests/conftest.py`. Add at the bottom:

```python
class FakeTcpManager:
    """Fake TcpManager that records commands and lets tests control state.

    Use in place of a mock when you want to verify *what* was sent without
    tying tests to mock call syntax. Tests can read `sent_commands` to see
    every command that was sent, in order.
    """
    def __init__(self, connected: bool = True) -> None:
        self.is_connected: bool = connected
        self.sent_commands: list = []
        self.on_disconnect = None

    async def send_command(self, cmd) -> None:
        if not self.is_connected:
            raise ConnectionError("Not connected")
        self.sent_commands.append(cmd)

    async def send(self, msg: str) -> None:
        pass

    async def disconnect(self) -> None:
        self.is_connected = False


@pytest.fixture
def fake_tcp():
    """Fresh FakeTcpManager per test, starts connected."""
    return FakeTcpManager(connected=True)
```

- [ ] **Step 4: Run the test to verify it passes**

Run:

```bash
cd c:/Users/thedo/git/spinlab && python -m pytest tests/test_fake_tcp.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
cd c:/Users/thedo/git/spinlab && git add tests/conftest.py tests/test_fake_tcp.py && git commit -m "test: add FakeTcpManager fixture for controller tests

Reusable fake that records sent commands by type. Used by upcoming
capture_controller and session_manager tests to verify behavior
without mock-call-syntax tautologies."
```

---

## Task 4: test_capture_controller.py (new file)

**Files:**
- Create: `tests/test_capture_controller.py`

Dedicated coverage for `capture_controller.py` is currently **0%**. This task closes that entirely by testing the controller against a real in-memory `Database` and the `FakeTcpManager` fixture.

**Note on database fixture:** Use `Database(tmp_path / "test.db")` — SpinLab's `Database` class takes a file path. `tmp_path` per-test is cheap enough and isolates each test.

- [ ] **Step 1: Write the test file skeleton + first test (start_reference guards)**

Create `tests/test_capture_controller.py`:

```python
"""Tests for CaptureController orchestration logic.

Uses a real SQLite Database (tmp_path) and FakeTcpManager to exercise the
controller's real interactions with the DB schema and TCP protocol.
Mocking both collaborators would reduce these to tautology tests.
"""
import pytest

from spinlab.capture_controller import CaptureController
from spinlab.db import Database
from spinlab.models import Mode, Status
from spinlab.protocol import (
    ReferenceStartCmd, ReferenceStopCmd, ReplayCmd, ReplayStopCmd,
    FillGapLoadCmd,
)


@pytest.fixture
def db(tmp_path):
    """Real in-memory-ish SQLite database, per-test."""
    d = Database(tmp_path / "test.db")
    d.upsert_game("g1", "Test Game", "any%")
    return d


@pytest.fixture
def controller(db, fake_tcp):
    return CaptureController(db, fake_tcp)


class TestStartReference:
    async def test_guard_draft_pending(self, controller, tmp_path):
        # Put the controller's draft manager into "has draft" state by
        # simulating a prior capture flow.
        controller.draft.entered = True  # direct flag: DraftManager has this
        result = await controller.start_reference(
            Mode.IDLE, "g1", tmp_path, run_name="test"
        )
        assert result.status == Status.DRAFT_PENDING

    async def test_guard_practice_active(self, controller, tmp_path):
        result = await controller.start_reference(
            Mode.PRACTICE, "g1", tmp_path
        )
        assert result.status == Status.PRACTICE_ACTIVE

    async def test_guard_already_replaying(self, controller, tmp_path):
        result = await controller.start_reference(
            Mode.REPLAY, "g1", tmp_path
        )
        assert result.status == Status.ALREADY_REPLAYING

    async def test_guard_not_connected(self, controller, tmp_path, fake_tcp):
        fake_tcp.is_connected = False
        result = await controller.start_reference(
            Mode.IDLE, "g1", tmp_path
        )
        assert result.status == Status.NOT_CONNECTED

    async def test_happy_path(self, controller, tmp_path, fake_tcp):
        result = await controller.start_reference(
            Mode.IDLE, "g1", tmp_path, run_name="my run"
        )
        assert result.status == Status.STARTED
        assert result.new_mode == Mode.REFERENCE
        assert len(fake_tcp.sent_commands) == 1
        assert isinstance(fake_tcp.sent_commands[0], ReferenceStartCmd)
        assert controller.ref_capture.capture_run_id is not None
```

**Note about `controller.draft.entered`:** Before relying on this in the test, verify the actual attribute name. Run:

```bash
cd c:/Users/thedo/git/spinlab && python -c "from spinlab.draft_manager import DraftManager; d = DraftManager(); print([a for a in dir(d) if not a.startswith('_')])"
```

Then adjust the test to use whatever attribute or method sets "has_draft=True" on a fresh DraftManager. If there's no direct setter, use a small helper: populate the DB with a draft capture_run row, then call `controller.recover_draft("g1")` to set has_draft.

- [ ] **Step 2: Run Step 1's tests and verify they pass**

Run:

```bash
cd c:/Users/thedo/git/spinlab && python -m pytest tests/test_capture_controller.py::TestStartReference -v
```

Expected: all 5 tests pass. Fix the `draft_pending` test's "has draft" setup if needed.

- [ ] **Step 3: Add stop_reference tests**

Append to `tests/test_capture_controller.py`:

```python
class TestStopReference:
    async def test_not_in_reference(self, controller):
        result = await controller.stop_reference(Mode.IDLE)
        assert result.status == Status.NOT_IN_REFERENCE

    async def test_happy_path_enters_draft(self, controller, tmp_path, db, fake_tcp):
        # Start reference first so capture_run_id is set
        await controller.start_reference(Mode.IDLE, "g1", tmp_path)
        run_id = controller.ref_capture.capture_run_id
        # Simulate some captured segments so _enter_draft_from_capture has work
        controller.ref_capture.segment_times = []  # empty is fine; just exercise the path

        result = await controller.stop_reference(Mode.REFERENCE)

        assert result.status == Status.STOPPED
        assert result.new_mode == Mode.IDLE
        # ReferenceStopCmd sent
        stop_cmds = [c for c in fake_tcp.sent_commands if isinstance(c, ReferenceStopCmd)]
        assert len(stop_cmds) == 1
```

- [ ] **Step 4: Run and verify**

```bash
cd c:/Users/thedo/git/spinlab && python -m pytest tests/test_capture_controller.py::TestStopReference -v
```

Expected: 2 passed.

- [ ] **Step 5: Add start_replay / stop_replay tests**

Append to `tests/test_capture_controller.py`:

```python
class TestStartReplay:
    async def test_guard_reference_active(self, controller):
        result = await controller.start_replay(Mode.REFERENCE, "g1", "/tmp/foo.spinrec")
        assert result.status == Status.REFERENCE_ACTIVE

    async def test_guard_already_replaying(self, controller):
        result = await controller.start_replay(Mode.REPLAY, "g1", "/tmp/foo.spinrec")
        assert result.status == Status.ALREADY_REPLAYING

    async def test_happy_path(self, controller, fake_tcp):
        result = await controller.start_replay(Mode.IDLE, "g1", "/tmp/foo.spinrec", speed=2)
        assert result.status == Status.STARTED
        assert result.new_mode == Mode.REPLAY
        replay_cmds = [c for c in fake_tcp.sent_commands if isinstance(c, ReplayCmd)]
        assert len(replay_cmds) == 1
        assert replay_cmds[0].path == "/tmp/foo.spinrec"
        assert replay_cmds[0].speed == 2


class TestStopReplay:
    async def test_not_replaying(self, controller):
        result = await controller.stop_replay(Mode.IDLE)
        assert result.status == Status.NOT_REPLAYING

    async def test_no_segments_hard_deletes_run(self, controller, db, fake_tcp):
        await controller.start_replay(Mode.IDLE, "g1", "/tmp/foo.spinrec")
        run_id = controller.ref_capture.capture_run_id
        # No segments captured → stop should hard-delete the capture_run
        result = await controller.stop_replay(Mode.REPLAY)
        assert result.status == Status.STOPPED
        # Verify the run was deleted from the DB
        row = db.conn.execute(
            "SELECT id FROM capture_runs WHERE id = ?", (run_id,)
        ).fetchone()
        assert row is None, "capture_run should have been hard-deleted"
```

- [ ] **Step 6: Run and verify**

```bash
cd c:/Users/thedo/git/spinlab && python -m pytest tests/test_capture_controller.py::TestStartReplay tests/test_capture_controller.py::TestStopReplay -v
```

Expected: 5 passed.

**Note:** If `db.conn.execute("SELECT id FROM capture_runs ...")` gives an error, check the actual table name with:

```bash
python -c "from spinlab.db import Database; import tempfile; d = Database(tempfile.mktemp(suffix='.db')); print([r[0] for r in d.conn.execute(\"SELECT name FROM sqlite_master WHERE type='table'\").fetchall()])"
```

Adjust table name if needed.

- [ ] **Step 7: Add handle_replay_error and handle_disconnect tests**

Append to `tests/test_capture_controller.py`:

```python
class TestHandleReplayError:
    async def test_no_segments_deletes_run(self, controller, db):
        await controller.start_replay(Mode.IDLE, "g1", "/tmp/foo.spinrec")
        run_id = controller.ref_capture.capture_run_id
        controller.handle_replay_error()
        row = db.conn.execute(
            "SELECT id FROM capture_runs WHERE id = ?", (run_id,)
        ).fetchone()
        assert row is None


class TestHandleDisconnect:
    async def test_no_segments_deletes_run(self, controller, db, tmp_path):
        await controller.start_reference(Mode.IDLE, "g1", tmp_path)
        run_id = controller.ref_capture.capture_run_id
        controller.handle_disconnect()
        row = db.conn.execute(
            "SELECT id FROM capture_runs WHERE id = ?", (run_id,)
        ).fetchone()
        assert row is None

    async def test_idempotent_when_nothing_active(self, controller):
        # No exception when no capture is in progress
        controller.handle_disconnect()
```

- [ ] **Step 8: Run and verify**

```bash
cd c:/Users/thedo/git/spinlab && python -m pytest tests/test_capture_controller.py::TestHandleReplayError tests/test_capture_controller.py::TestHandleDisconnect -v
```

Expected: 3 passed.

- [ ] **Step 9: Add fill_gap tests**

Append to `tests/test_capture_controller.py`:

```python
class TestStartFillGap:
    async def test_not_connected(self, controller, fake_tcp):
        fake_tcp.is_connected = False
        result = await controller.start_fill_gap("seg1")
        assert result.status == Status.NOT_CONNECTED

    async def test_no_hot_variant(self, controller, db):
        # Insert a segment with no hot save state
        from spinlab.models import Segment, Waypoint, WaypointSaveState
        wp_start = Waypoint.make("g1", 1, "entrance", 0, {})
        wp_end = Waypoint.make("g1", 1, "goal", 0, {})
        db.upsert_waypoint(wp_start)
        db.upsert_waypoint(wp_end)
        seg = Segment(
            id="seg1", game_id="g1", level_number=1,
            start_type="entrance", start_ordinal=0,
            end_type="goal", end_ordinal=0,
            start_waypoint_id=wp_start.id, end_waypoint_id=wp_end.id,
        )
        db.upsert_segment(seg)
        # No add_save_state call → no hot variant

        result = await controller.start_fill_gap("seg1")
        assert result.status == Status.NO_HOT_VARIANT

    async def test_happy_path(self, controller, db, tmp_path, fake_tcp):
        from spinlab.models import Segment, Waypoint, WaypointSaveState
        wp_start = Waypoint.make("g1", 1, "entrance", 0, {})
        wp_end = Waypoint.make("g1", 1, "goal", 0, {})
        db.upsert_waypoint(wp_start)
        db.upsert_waypoint(wp_end)
        seg = Segment(
            id="seg1", game_id="g1", level_number=1,
            start_type="entrance", start_ordinal=0,
            end_type="goal", end_ordinal=0,
            start_waypoint_id=wp_start.id, end_waypoint_id=wp_end.id,
        )
        db.upsert_segment(seg)
        state_file = tmp_path / "hot.mss"
        state_file.write_bytes(b"fake")
        db.add_save_state(WaypointSaveState(
            waypoint_id=wp_start.id, variant_type="hot",
            state_path=str(state_file), is_default=True,
        ))

        result = await controller.start_fill_gap("seg1")
        assert result.status == Status.STARTED
        assert result.new_mode == Mode.FILL_GAP
        fill_cmds = [c for c in fake_tcp.sent_commands if isinstance(c, FillGapLoadCmd)]
        assert len(fill_cmds) == 1
        assert fill_cmds[0].state_path == str(state_file)
```

- [ ] **Step 10: Run and verify**

```bash
cd c:/Users/thedo/git/spinlab && python -m pytest tests/test_capture_controller.py::TestStartFillGap -v
```

Expected: 3 passed.

- [ ] **Step 11: Run the whole new file and the fast suite**

```bash
cd c:/Users/thedo/git/spinlab && python -m pytest tests/test_capture_controller.py -v
```

Expected: ~13 tests pass.

```bash
cd c:/Users/thedo/git/spinlab && python -m pytest -m "not (emulator or slow or frontend)" -q
```

Expected: previous ~509 + ~13 new + ~3 fake_tcp = ~525 pass. No failures.

- [ ] **Step 12: Verify dedicated coverage improved**

```bash
cd c:/Users/thedo/git/spinlab && python scripts/dedicated_coverage.py
```

Expected: `capture_controller` dedicated % jumped from ~0% to above 80%.

- [ ] **Step 13: Commit**

```bash
cd c:/Users/thedo/git/spinlab && git add tests/test_capture_controller.py && git commit -m "test: add dedicated tests for CaptureController

Previously CaptureController had 0% dedicated coverage — all coverage
came from tests that exercised it incidentally through session_manager
or dashboard integration. This adds focused tests for:

- start_reference / stop_reference guards and happy paths
- start_replay / stop_replay including the 'no segments → hard delete
  capture_run' branch
- handle_replay_error and handle_disconnect branching
- start_fill_gap guards and happy path

Tests use real in-memory SQLite + FakeTcpManager rather than mocks,
so they verify behavior against the real DB schema."
```

---

## Task 5: test_session_manager.py lifecycle additions

**Files:**
- Modify: `tests/test_session_manager.py`
- Modify: `python/spinlab/session_manager.py` (maybe — only if needed to make PRACTICE_STOP_TIMEOUT_S injectable)

Existing tests cover event routing and mode guards. Missing: `start_practice` / `stop_practice` / `start_speed_run` / `stop_speed_run` / `on_disconnect` / `shutdown`.

**Short-timeout approach:** Rather than modifying production code, monkeypatch the module constant in tests. This is the standard pattern and avoids coupling production code to test concerns.

- [ ] **Step 1: Write the start_practice / stop_practice test class**

Append to `tests/test_session_manager.py` (after existing classes):

```python
import asyncio as _asyncio  # alias to avoid conflicting with top-of-file imports

from spinlab import session_manager as session_manager_module


class TestPracticeLifecycle:
    """Tests for start_practice, stop_practice, and _on_practice_done."""

    async def test_start_practice_blocked_by_draft(self, mock_db, mock_tcp):
        sm = make_sm(mock_db, mock_tcp)
        sm.game_id = "game1"
        sm.capture.draft.entered = True  # simulate has_draft=True
        result = await sm.start_practice()
        assert result.status == Status.DRAFT_PENDING

    async def test_start_practice_blocked_by_not_connected(self, mock_db, mock_tcp):
        sm = make_sm(mock_db, mock_tcp)
        sm.game_id = "game1"
        mock_tcp.is_connected = False
        result = await sm.start_practice()
        assert result.status == Status.NOT_CONNECTED

    async def test_start_practice_blocked_when_already_running(self, mock_db, mock_tcp):
        sm = make_sm(mock_db, mock_tcp)
        sm.game_id = "game1"
        # Fake an already-running practice session
        fake_ps = MagicMock()
        fake_ps.is_running = True
        sm.practice_session = fake_ps
        result = await sm.start_practice()
        assert result.status == Status.ALREADY_RUNNING

    async def test_stop_practice_when_not_running(self, mock_db, mock_tcp):
        sm = make_sm(mock_db, mock_tcp)
        result = await sm.stop_practice()
        assert result.status == Status.NOT_RUNNING

    async def test_stop_practice_clears_stale_mode(self, mock_db, mock_tcp):
        """If mode=PRACTICE but no session, stop_practice should still reset."""
        sm = make_sm(mock_db, mock_tcp)
        sm.mode = Mode.PRACTICE
        result = await sm.stop_practice()
        assert result.status == Status.STOPPED
        assert sm.mode == Mode.IDLE

    async def test_stop_practice_cancels_hung_task(self, mock_db, mock_tcp, monkeypatch):
        """When practice task doesn't exit on is_running=False, stop should cancel
        after the timeout elapses."""
        monkeypatch.setattr(session_manager_module, "PRACTICE_STOP_TIMEOUT_S", 0.1)

        sm = make_sm(mock_db, mock_tcp)
        sm.game_id = "game1"
        sm.mode = Mode.PRACTICE

        # Set up a fake practice session whose task never exits
        fake_ps = MagicMock()
        fake_ps.is_running = True
        sm.practice_session = fake_ps

        # Create a task that sleeps forever — mimics a hung practice loop
        async def hung_loop():
            await _asyncio.sleep(10)

        sm.practice_task = _asyncio.create_task(hung_loop())

        result = await sm.stop_practice()

        assert result.status == Status.STOPPED
        assert sm.mode == Mode.IDLE
        assert sm.practice_task.cancelled() or sm.practice_task.done()
```

**Note on `sm.capture.draft.entered`:** verify this is the real attribute name by running:

```bash
cd c:/Users/thedo/git/spinlab && python -c "from spinlab.draft_manager import DraftManager; d = DraftManager(); print([a for a in dir(d) if not a.startswith('_')]); print('has_draft:', d.has_draft)"
```

Adjust to whatever internal flag `has_draft` reads. If `has_draft` reads from a DB query, then use the "populate DB + recover_draft" pattern instead.

- [ ] **Step 2: Run and verify**

```bash
cd c:/Users/thedo/git/spinlab && python -m pytest tests/test_session_manager.py::TestPracticeLifecycle -v
```

Expected: 6 passed. The `test_stop_practice_cancels_hung_task` test should take <0.5s thanks to the monkeypatched timeout.

- [ ] **Step 3: Add speed_run lifecycle tests**

Append to `tests/test_session_manager.py`:

```python
class TestSpeedRunLifecycle:
    """Tests for start_speed_run, stop_speed_run, and _on_speed_run_done."""

    async def test_start_blocked_by_draft(self, mock_db, mock_tcp):
        sm = make_sm(mock_db, mock_tcp)
        sm.game_id = "game1"
        sm.capture.draft.entered = True
        result = await sm.start_speed_run()
        assert result.status == Status.DRAFT_PENDING

    async def test_start_blocked_by_not_connected(self, mock_db, mock_tcp):
        sm = make_sm(mock_db, mock_tcp)
        sm.game_id = "game1"
        mock_tcp.is_connected = False
        result = await sm.start_speed_run()
        assert result.status == Status.NOT_CONNECTED

    async def test_start_missing_save_states(self, mock_db, mock_tcp):
        """SpeedRunSession._finalize_level raises ValueError when a segment
        has no save_state path on disk; start_speed_run translates that to
        MISSING_SAVE_STATES."""
        sm = make_sm(mock_db, mock_tcp)
        sm.game_id = "game1"
        # Return a single segment whose state_path is bogus → _finalize_level
        # raises ValueError → start_speed_run returns MISSING_SAVE_STATES.
        mock_db.get_all_segments_with_model.return_value = [{
            "id": "seg1",
            "game_id": "game1",
            "level_number": 1,
            "start_type": "entrance",
            "start_ordinal": 0,
            "end_type": "goal",
            "end_ordinal": 0,
            "description": "L1",
            "state_path": "/definitely/not/a/real/path.mss",
        }]
        result = await sm.start_speed_run()
        assert result.status == Status.MISSING_SAVE_STATES

    async def test_stop_when_not_running(self, mock_db, mock_tcp):
        sm = make_sm(mock_db, mock_tcp)
        result = await sm.stop_speed_run()
        assert result.status == Status.NOT_RUNNING

    async def test_stop_clears_stale_mode(self, mock_db, mock_tcp):
        sm = make_sm(mock_db, mock_tcp)
        sm.mode = Mode.SPEED_RUN
        result = await sm.stop_speed_run()
        assert result.status == Status.STOPPED
        assert sm.mode == Mode.IDLE
```

**Note:** `test_start_missing_save_states` is defensive — the exact behavior depends on whether `SpeedRunSession.__init__` raises when `_build_levels` returns empty. Read `python/spinlab/speed_run.py::SpeedRunSession.__init__` to confirm before finalizing. If it doesn't raise, either remove this test or update it to make `_build_levels` actually fail (e.g., by providing segments without save state paths).

- [ ] **Step 4: Run and verify**

```bash
cd c:/Users/thedo/git/spinlab && python -m pytest tests/test_session_manager.py::TestSpeedRunLifecycle -v
```

Expected: 5 passed. If `test_start_missing_save_states` is ambiguous, fix it based on the speed_run.py behavior.

- [ ] **Step 5: Add disconnect / shutdown tests**

Append to `tests/test_session_manager.py`:

```python
class TestDisconnectAndShutdown:
    def test_on_disconnect_stops_practice(self, mock_db, mock_tcp):
        sm = make_sm(mock_db, mock_tcp)
        fake_ps = MagicMock()
        fake_ps.is_running = True
        sm.practice_session = fake_ps
        sm.on_disconnect()
        assert fake_ps.is_running is False

    def test_on_disconnect_stops_speed_run(self, mock_db, mock_tcp):
        sm = make_sm(mock_db, mock_tcp)
        fake_sr = MagicMock()
        fake_sr.is_running = True
        sm.speed_run_session = fake_sr
        sm.on_disconnect()
        assert fake_sr.is_running is False

    def test_on_disconnect_clears_ref_and_idles(self, mock_db, mock_tcp):
        sm = make_sm(mock_db, mock_tcp)
        sm.mode = Mode.REFERENCE
        sm.on_disconnect()
        assert sm.mode == Mode.IDLE

    async def test_shutdown_stops_practice_and_tcp(self, mock_db, mock_tcp):
        sm = make_sm(mock_db, mock_tcp)
        sm.mode = Mode.PRACTICE  # stale — no real session
        await sm.shutdown()
        mock_tcp.disconnect.assert_called_once()

    async def test_shutdown_clears_reference(self, mock_db, mock_tcp):
        sm = make_sm(mock_db, mock_tcp)
        sm.mode = Mode.REFERENCE
        await sm.shutdown()
        assert sm.mode == Mode.IDLE
        mock_tcp.disconnect.assert_called_once()
```

**Note:** `mock_tcp.disconnect` is an `AsyncMock` in the existing fixture — check `tests/conftest.py`. If it's a plain MagicMock, add:

```python
mock_tcp.disconnect = AsyncMock()
```

at the top of the tests that need it. Simplest: add it in the fixture in conftest.py (one-line change).

- [ ] **Step 6: Run and verify**

```bash
cd c:/Users/thedo/git/spinlab && python -m pytest tests/test_session_manager.py::TestDisconnectAndShutdown -v
```

Expected: 5 passed. If `mock_tcp.disconnect` fails with "object is not awaitable," add `disconnect=AsyncMock()` to the `mock_tcp` fixture in `tests/conftest.py`.

- [ ] **Step 7: Run the whole session_manager test file**

```bash
cd c:/Users/thedo/git/spinlab && python -m pytest tests/test_session_manager.py -v
```

Expected: all previous tests pass + ~16 new tests.

- [ ] **Step 8: Verify dedicated coverage improved**

```bash
cd c:/Users/thedo/git/spinlab && python scripts/dedicated_coverage.py
```

Expected: `session_manager` dedicated % jumped from ~61% to above 80%.

- [ ] **Step 9: Commit**

```bash
cd c:/Users/thedo/git/spinlab && git add tests/test_session_manager.py tests/conftest.py && git commit -m "test: add lifecycle coordination tests for SessionManager

Fills a gap where session_manager dedicated coverage was 61% despite
suite-wide 77%. The missing 16% was all async coordination:

- start_practice / stop_practice guards, happy path, and the
  cancel-on-timeout path (monkeypatched to 100ms)
- start_speed_run / stop_speed_run guards and stale-mode cleanup
- on_disconnect halting practice + speed run + clearing reference
- shutdown calling stop_practice, stop_speed_run, and tcp.disconnect"
```

---

## Task 6: test_state_builder.py (new file)

**Files:**
- Create: `tests/test_state_builder.py`

Cover only branches that `test_dashboard_integration.py` doesn't already exercise: speed_run, cold_fill, draft, and idle/no-game base case. The practice branch is already covered end-to-end.

- [ ] **Step 1: Write the test file with idle and speed_run branches**

Create `tests/test_state_builder.py`:

```python
"""Tests for StateBuilder — covers branches not already exercised by
test_dashboard_integration.py (which covers the practice branch).
"""
from unittest.mock import MagicMock

import pytest

from spinlab.models import Mode
from spinlab.state_builder import StateBuilder


class TestIdleBaseCase:
    def test_no_game_returns_bare_state(self, mock_db):
        sb = StateBuilder(mock_db)
        sm = MagicMock()
        sm.mode = Mode.IDLE
        sm.tcp.is_connected = True
        sm.game_id = None
        sm.game_name = None
        sm.capture.sections_captured = 0

        state = sb.build(sm)

        assert state["mode"] == "idle"
        assert state["game_id"] is None
        assert state["game_name"] is None
        assert state["current_segment"] is None
        assert state["recent"] == []
        assert state["session"] is None
        assert state["allocator_weights"] is None
        assert state["estimator"] is None


class TestSpeedRunBranch:
    def test_speed_run_populates_current_level(self, mock_db):
        sb = StateBuilder(mock_db)
        sm = MagicMock()
        sm.mode = Mode.SPEED_RUN
        sm.tcp.is_connected = True
        sm.game_id = "g1"
        sm.game_name = "Test"
        sm.capture.sections_captured = 0
        sm.capture.get_draft_state.return_value = None

        # Fake scheduler so the base state gets built
        fake_sched = MagicMock()
        fake_sched.allocator.entries = []
        fake_sched.estimator.name = "kalman"
        sm._get_scheduler.return_value = fake_sched

        # Fake speed_run_session with one level
        fake_level = MagicMock()
        fake_level.level_number = 5
        fake_level.description = "Level 5"
        fake_level.entrance_state_path = "/tmp/l5.mss"
        fake_level.segments = [{"id": "seg-l5"}]

        sr = MagicMock()
        sr.session_id = "sr-abc"
        sr.started_at = "2026-04-10T12:00:00"
        sr.segments_recorded = 3
        sr.levels_completed = 2
        sr.current_level_index = 0
        sr.levels = [fake_level]
        sr.game_id = "g1"
        sm.speed_run_session = sr

        state = sb.build(sm)

        assert state["mode"] == "speed_run"
        assert state["session"]["id"] == "sr-abc"
        assert state["session"]["segments_attempted"] == 3
        assert state["session"]["segments_completed"] == 2
        assert state["current_segment"]["level_number"] == 5
        assert state["current_segment"]["description"] == "Level 5"
        assert state["current_segment"]["state_path"] == "/tmp/l5.mss"
        assert state["current_segment"]["id"] == "seg-l5"
```

- [ ] **Step 2: Run and verify**

```bash
cd c:/Users/thedo/git/spinlab && python -m pytest tests/test_state_builder.py -v
```

Expected: 2 passed.

- [ ] **Step 3: Add cold_fill and draft branch tests**

Append to `tests/test_state_builder.py`:

```python
class TestColdFillBranch:
    def test_cold_fill_includes_state(self, mock_db):
        sb = StateBuilder(mock_db)
        sm = MagicMock()
        sm.mode = Mode.COLD_FILL
        sm.tcp.is_connected = True
        sm.game_id = "g1"
        sm.game_name = "Test"
        sm.capture.sections_captured = 0
        sm.capture.get_draft_state.return_value = None

        fake_sched = MagicMock()
        fake_sched.allocator.entries = []
        fake_sched.estimator.name = "kalman"
        sm._get_scheduler.return_value = fake_sched

        sm.cold_fill.get_state.return_value = {
            "current_segment_id": "seg1",
            "remaining": 3,
            "total": 5,
        }

        state = sb.build(sm)
        assert state["mode"] == "cold_fill"
        assert state["cold_fill"]["remaining"] == 3
        assert state["cold_fill"]["total"] == 5

    def test_cold_fill_none_state_omitted(self, mock_db):
        """When cold_fill.get_state() returns None, no cold_fill key is added."""
        sb = StateBuilder(mock_db)
        sm = MagicMock()
        sm.mode = Mode.COLD_FILL
        sm.tcp.is_connected = True
        sm.game_id = "g1"
        sm.game_name = "Test"
        sm.capture.sections_captured = 0
        sm.capture.get_draft_state.return_value = None

        fake_sched = MagicMock()
        fake_sched.allocator.entries = []
        fake_sched.estimator.name = "kalman"
        sm._get_scheduler.return_value = fake_sched

        sm.cold_fill.get_state.return_value = None

        state = sb.build(sm)
        assert "cold_fill" not in state


class TestDraftBranch:
    def test_draft_state_included_when_active(self, mock_db):
        sb = StateBuilder(mock_db)
        sm = MagicMock()
        sm.mode = Mode.IDLE
        sm.tcp.is_connected = True
        sm.game_id = "g1"
        sm.game_name = "Test"
        sm.capture.sections_captured = 7

        fake_sched = MagicMock()
        fake_sched.allocator.entries = []
        fake_sched.estimator.name = "kalman"
        sm._get_scheduler.return_value = fake_sched

        sm.capture.get_draft_state.return_value = {
            "run_id": "run-xyz", "segment_count": 7,
        }

        state = sb.build(sm)
        assert state["draft"] == {"run_id": "run-xyz", "segment_count": 7}
```

- [ ] **Step 4: Run and verify**

```bash
cd c:/Users/thedo/git/spinlab && python -m pytest tests/test_state_builder.py -v
```

Expected: 5 passed.

- [ ] **Step 5: Verify dedicated coverage improved**

```bash
cd c:/Users/thedo/git/spinlab && python scripts/dedicated_coverage.py
```

Expected: `state_builder` dedicated % jumped from ~38% to above 70%.

- [ ] **Step 6: Commit**

```bash
cd c:/Users/thedo/git/spinlab && git add tests/test_state_builder.py && git commit -m "test: add dedicated tests for StateBuilder

Covers branches that test_dashboard_integration doesn't already
exercise: idle/no-game base case, speed_run level population,
cold_fill state inclusion/exclusion, and draft state inclusion.

The practice branch is skipped because dashboard_integration already
exercises it end-to-end through the HTTP layer."
```

---

## Task 7: test_scheduler_kalman.py additions

**Files:**
- Modify: `tests/test_scheduler_kalman.py`

Add tests for `_sync_config_from_db`, `rebuild_all_states`, and `set_allocator_weights` validation.

- [ ] **Step 1: Add _sync_config_from_db test**

First read the existing file to see its fixture pattern:

```bash
cd c:/Users/thedo/git/spinlab && python -c "import tests.test_scheduler_kalman; print([x for x in dir(tests.test_scheduler_kalman) if not x.startswith('_')])"
```

Append to `tests/test_scheduler_kalman.py`:

```python
class TestSyncConfigFromDb:
    def test_allocator_weights_change_detected(self, db_with_segments):
        """Changing weights in the DB between pick_next calls should rebuild
        the allocator."""
        import json
        from spinlab.scheduler import Scheduler

        sched = Scheduler(db_with_segments, "g1")
        initial_weights_json = sched._weights_json

        # Change weights in the DB directly
        new_weights = {"greedy": 100}
        db_with_segments.save_allocator_config(
            "allocator_weights", json.dumps(new_weights)
        )

        # Next pick should detect the change
        sched.pick_next()
        assert sched._weights_json != initial_weights_json
        assert json.loads(sched._weights_json) == new_weights

    def test_estimator_change_detected(self, db_with_segments):
        """Changing the estimator in the DB should update sched.estimator."""
        from spinlab.estimators import list_estimators
        from spinlab.scheduler import Scheduler

        sched = Scheduler(db_with_segments, "g1")
        initial_name = sched.estimator.name

        # Pick a different estimator name
        other = [n for n in list_estimators() if n != initial_name]
        if not other:
            pytest.skip("Only one estimator registered — can't test switch")
        new_name = other[0]

        db_with_segments.save_allocator_config("estimator", new_name)
        sched.pick_next()
        assert sched.estimator.name == new_name


class TestSetAllocatorWeights:
    def test_sum_must_equal_100(self, db_with_segments):
        from spinlab.scheduler import Scheduler
        sched = Scheduler(db_with_segments, "g1")
        with pytest.raises(ValueError, match="sum to 100"):
            sched.set_allocator_weights({"greedy": 50, "random": 30})

    def test_unknown_allocator_name_rejected(self, db_with_segments):
        from spinlab.scheduler import Scheduler
        sched = Scheduler(db_with_segments, "g1")
        with pytest.raises(ValueError, match="Unknown allocator"):
            sched.set_allocator_weights({"greedy": 50, "not_a_real_allocator": 50})

    def test_valid_weights_persisted(self, db_with_segments):
        import json
        from spinlab.scheduler import Scheduler
        sched = Scheduler(db_with_segments, "g1")
        sched.set_allocator_weights({"greedy": 60, "random": 40})
        raw = db_with_segments.load_allocator_config("allocator_weights")
        assert json.loads(raw) == {"greedy": 60, "random": 40}


class TestRebuildAllStates:
    def test_rebuilds_from_attempt_history(self, db_with_segments):
        """After recording some attempts, rebuild_all_states should
        produce model states for each segment with attempts."""
        from spinlab.scheduler import Scheduler
        sched = Scheduler(db_with_segments, "g1")

        # Grab the first segment and log an attempt via scheduler
        segs = db_with_segments.get_all_segments_with_model("g1")
        assert segs, "fixture should provide segments"
        seg_id = segs[0]["id"]
        sched.process_attempt(seg_id, time_ms=5000, completed=True)

        # Clear the existing model_state row so rebuild has work to do
        # (or just verify rebuild doesn't error and still produces a row)
        sched.rebuild_all_states()

        row = db_with_segments.load_model_state(seg_id, sched.estimator.name)
        assert row is not None
        assert row["state_json"]
```

**Note on allocator names:** The `test_sum_must_equal_100` and others assume allocators named `"greedy"` and `"random"` exist. Verify:

```bash
cd c:/Users/thedo/git/spinlab && python -c "from spinlab.allocators import list_allocators; print(list_allocators())"
```

Adjust names if they differ.

- [ ] **Step 2: Run and verify**

```bash
cd c:/Users/thedo/git/spinlab && python -m pytest tests/test_scheduler_kalman.py::TestSyncConfigFromDb tests/test_scheduler_kalman.py::TestSetAllocatorWeights tests/test_scheduler_kalman.py::TestRebuildAllStates -v
```

Expected: 7 passed (one may skip if only one estimator is registered).

- [ ] **Step 3: Verify dedicated coverage improved**

```bash
cd c:/Users/thedo/git/spinlab && python scripts/dedicated_coverage.py
```

Expected: `scheduler` dedicated % jumped from ~38% to above 70%.

- [ ] **Step 4: Commit**

```bash
cd c:/Users/thedo/git/spinlab && git add tests/test_scheduler_kalman.py && git commit -m "test: add dedicated tests for Scheduler config sync and rebuild

Covers _sync_config_from_db (detecting allocator weight and estimator
changes between picks), set_allocator_weights validation (sum must be
100, unknown allocator rejected), and rebuild_all_states happy path.

These were all reached incidentally from practice/dashboard tests but
had no dedicated scheduler-level verification."
```

---

## Task 8: test_tcp_manager.py additions

**Files:**
- Modify: `tests/test_tcp_manager.py`

The existing file already has `_read_loop` tests for ok:/err:/pong. Missing: connection-closed branch of `_read_loop` and `disconnect` behavior (task cancel + queue drain).

- [ ] **Step 1: Add connection closed test**

Append to `tests/test_tcp_manager.py`:

```python
@pytest.mark.asyncio
async def test_read_loop_exits_on_empty_line(caplog):
    """Empty bytes from readline means remote closed the connection — loop
    should exit cleanly (no exception, logs info)."""
    import logging

    manager = _make_manager_with_lines([])  # empty → first readline returns b""
    with caplog.at_level(logging.INFO, logger="spinlab.tcp_manager"):
        await manager._read_loop()

    messages = [r.message for r in caplog.records]
    assert any("closed by remote" in m for m in messages), (
        f"Expected 'closed by remote' log, got: {messages}"
    )


@pytest.mark.asyncio
async def test_read_loop_cleans_up_on_exit():
    """After _read_loop exits, _reader and _writer should be None."""
    manager = _make_manager_with_lines([])
    await manager._read_loop()
    assert manager._reader is None
    assert manager._writer is None
```

- [ ] **Step 2: Run and verify**

```bash
cd c:/Users/thedo/git/spinlab && python -m pytest tests/test_tcp_manager.py::test_read_loop_exits_on_empty_line tests/test_tcp_manager.py::test_read_loop_cleans_up_on_exit -v
```

Expected: 2 passed.

- [ ] **Step 3: Add on_disconnect callback test**

Append to `tests/test_tcp_manager.py`:

```python
@pytest.mark.asyncio
async def test_on_disconnect_callback_fires():
    """When the read loop exits, the on_disconnect callback should be called."""
    manager = _make_manager_with_lines([])
    called = []
    manager.on_disconnect = lambda: called.append(True)

    await manager._read_loop()
    assert called == [True]
```

- [ ] **Step 4: Add disconnect() method test**

Append to `tests/test_tcp_manager.py`:

```python
@pytest.mark.asyncio
async def test_disconnect_drains_events_queue(tcp_server):
    """disconnect() should drain any pending events from the queue."""
    srv, port = tcp_server
    mgr = TcpManager("127.0.0.1", port)
    await mgr.connect()
    conn, _ = srv.accept()

    # Stuff the queue with some pending events
    await mgr.events.put({"event": "dummy1"})
    await mgr.events.put({"event": "dummy2"})
    assert mgr.events.qsize() == 2

    conn.close()
    await mgr.disconnect()

    assert mgr.events.qsize() == 0
    assert not mgr.is_connected
```

- [ ] **Step 5: Run all new tests**

```bash
cd c:/Users/thedo/git/spinlab && python -m pytest tests/test_tcp_manager.py -v
```

Expected: previous tests + 4 new tests all pass.

- [ ] **Step 6: Verify dedicated coverage improved**

```bash
cd c:/Users/thedo/git/spinlab && python scripts/dedicated_coverage.py
```

Expected: `tcp_manager` dedicated % above 75%.

- [ ] **Step 7: Commit**

```bash
cd c:/Users/thedo/git/spinlab && git add tests/test_tcp_manager.py && git commit -m "test: add tests for TcpManager disconnect and cleanup paths

Covers:
- _read_loop exit on empty-line (remote closed) with correct log
- _reader/_writer cleanup after loop exits
- on_disconnect callback fires when loop exits
- disconnect() drains pending events from the queue"
```

---

## Task 9: Final verification

**Files:** none modified

- [ ] **Step 1: Run the full test suite**

```bash
cd c:/Users/thedo/git/spinlab && python -m pytest
```

Expected: **all** tests pass — unit, slow, emulator (if Mesen available), and frontend. No failures, no new warnings.

If emulator tests can't run locally (Mesen not installed), note that in the handoff but still run everything else:

```bash
cd c:/Users/thedo/git/spinlab && python -m pytest -m "not emulator"
```

- [ ] **Step 2: Run dedicated coverage and capture the final table**

```bash
cd c:/Users/thedo/git/spinlab && python scripts/dedicated_coverage.py
```

Expected final state: all five target modules have dedicated coverage meaningfully closer to suite-wide. Specifically:
- `capture_controller`: was 0% → should be ≥80%
- `session_manager`: was 61% → should be ≥75%
- `state_builder`: was 38% → should be ≥65%
- `scheduler`: was 38% → should be ≥65%
- `tcp_manager`: was 56% → should be ≥75%

These are guidelines, not gates. If a number falls short, the question is "is the uncovered code meaningful?" not "did we hit a threshold?"

- [ ] **Step 3: No commit needed — verification only**

The previous commits already landed all changes. Step 2's dedicated coverage output is the evidence that the plan's goal was met.

---

## Notes for the engineer

- **Database API quirks:** SpinLab's `Database` class wraps sqlite3 directly and exposes `db.conn` for raw queries. Method names to know: `upsert_game`, `upsert_waypoint`, `upsert_segment`, `add_save_state`, `save_allocator_config`, `load_allocator_config`, `create_capture_run`, `hard_delete_capture_run`, `get_all_segments_with_model`, `load_model_state`.

- **Async test style:** The existing codebase uses `async def test_...` without `@pytest.mark.asyncio` decorators — `asyncio_mode = "auto"` is set in `pyproject.toml`. Follow that pattern for new async tests. `test_tcp_manager.py` is the exception (it uses explicit `@pytest.mark.asyncio` because it predates the auto mode).

- **Imports in tests:** Prefer adding imports at the top of the file. Only use function-level imports if there's a circular import or conditional import reason — don't copy that pattern from the scheduler tests.

- **If a dedicated test approach is more trouble than it's worth:** For a single deeply-coupled path, it may be cheaper to let the dashboard_integration test continue covering it than to rebuild half the test harness. Don't force a dedicated test in those cases — document why in the commit message.

- **Fast test budget:** The fast suite currently runs in ~6-7 seconds. The tests added here should add less than 2 seconds total. If something blows that budget, investigate.

- **Windows path gotcha:** The coverage SQLite DB may store paths with either `/` or `\`. The `dedicated_coverage.py` script handles this with `LIKE '%basename'`. If queries behave oddly, print a few paths from the `file` table.
