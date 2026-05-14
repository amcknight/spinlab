# Integration test diagnostic hook expansion (D1+D2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expand `_collect_diagnostics` in `tests/integration/conftest.py` so the failure-report block fires for `run_scenario`-based tests (today the bulk of emulator coverage gets an empty block) and includes richer fields — RA process liveness, RA NCI status, poller flake counters, `state_version`, session mode — so an agent reading a failing integration-test report can diagnose without running the test interactively.

**Architecture:** Refactor `_collect_diagnostics` into two composable collectors (`_collect_dashboard_diagnostics`, `_collect_harness_diagnostics`) chosen by inspecting `item.funcargs` for known fixture names. `replay_ra_dashboard` is extended to yield its `SessionManager` as a 4th tuple element so the dashboard collector can introspect poller / orchestrator / session state directly (vs. trying to fetch internals over HTTP). `run_scenario` is extended to attach the `ra_harness` it depends on as `.harness` on the returned callable, so the harness collector can reach it via `funcargs["run_scenario"].harness`. Each collector is unit-tested with synthetic `pytest.Item`-shaped objects — no live RA required.

**Tech Stack:** pytest hookimpl, the existing `RAHarness` + `NCIClient` + `SessionManager`/`Poller`/`RAClient` introspection surface.

**Context for the executing engineer:** This plan was generated alongside `2026-05-14-rom-keyed-harness-hard-fail-skips.md` from the same /improve scan (`docs/superpowers/scans/2026-05-14-improve-1735.md`). The two plans are independent — neither blocks the other — but if both land in the same session, do the ROM-keyed plan first since it's the must-fix.

**Why expand instead of just adding the new fields:** Verifier confirmed (in the scan) that the current hook at `conftest.py:536` is literally `for fixture_name in ("replay_ra_dashboard",):` — a one-element tuple. Every `run_scenario` test (9 in `test_transitions.py`, plus harness isolation, plus practice smoke) gets `_collect_diagnostics()` returning `""`. That's the bulk of emulator coverage producing zero diagnostic context on failure today.

**Why a `session` element on `replay_ra_dashboard`:** The current fixture yields `(base_url, db, tmp_path)` so the hook can hit `/api/state` but can't reach `app.state.session.orchestrator.poller` for poll_count / `_read_failing`. Adding a 4th element (`SessionManager`) is the smallest extension that unlocks poller + state_version + mode introspection. Only `test_replay_fixture.py` destructures this tuple today; updating it is one line.

---

## File Structure

**Modified:**
- `tests/integration/conftest.py`:
  - Extend `replay_ra_dashboard` (currently yields `(base_url, db, tmp_path)`) to yield `(base_url, db, tmp_path, session)`.
  - Extend `run_scenario` to attach `.harness` to its returned `_run` callable.
  - Refactor `_collect_diagnostics` to dispatch on fixture availability.
- `tests/integration/test_replay_fixture.py`: update the tuple unpack from `base_url, db, tmp_path = replay_ra_dashboard` to `base_url, db, tmp_path, _session = replay_ra_dashboard` (one site; the test doesn't need the session itself).

**Created:**
- `tests/unit/integration/test_diagnostic_hook.py` — unit tests for `_collect_dashboard_diagnostics` and `_collect_harness_diagnostics` using synthetic `pytest.Item`-shaped MagicMocks. No real RA required.

**Not touched:**
- The `_RingHandler` ring buffer + its installation (existing).
- The `pytest_runtest_makereport` hookwrapper itself (existing — just calls into `_collect_diagnostics`).
- Production code: no changes to Poller, RAClient, SessionManager. We use the already-existing `poll_count`, `_read_failing`, `state_version`, and `mode` attributes (verified present during plan-writing).

---

## Implementation Tasks

### Task 1: Extend `replay_ra_dashboard` to yield `session`

**Files:**
- Modify: `tests/integration/conftest.py:488` (the `yield` line) and add `session` to the unpack/return shape.
- Modify: `tests/integration/test_replay_fixture.py` (tuple-destructure update; line TBD — find via grep).

- [ ] **Step 1.1: Update the fixture yield**

In `tests/integration/conftest.py`, find the line:

```python
yield base_url, db, tmp_path
```

(currently around line 488 in the `replay_ra_dashboard` fixture body, after the unpause+settle block).

Replace with:

```python
yield base_url, db, tmp_path, app.state.session
```

`app` is the FastAPI app created earlier in the same fixture body (around line 428: `app = create_app(db=db, config=config)`). `app.state.session` is the `SessionManager` (FastAPI's `app.state` is the documented attachment point — see `dashboard.py:create_app`).

- [ ] **Step 1.2: Update the test destructure site**

Run: `python -m pytest --collect-only tests/integration/test_replay_fixture.py 2>&1 | tail -5`

Then find every line in `tests/integration/test_replay_fixture.py` that destructures `replay_ra_dashboard`:

```bash
grep -n "replay_ra_dashboard" tests/integration/test_replay_fixture.py
```

There should be one site (a `base_url, db, tmp_path = replay_ra_dashboard` line inside the test method). Update it to:

```python
base_url, db, tmp_path, _session = replay_ra_dashboard
```

The leading underscore signals the test doesn't use the session itself — it's there for the diagnostic hook.

- [ ] **Step 1.3: Confirm the test still passes (skipping is OK at this point — the C1-C3 plan handles skips)**

Run: `python -m pytest tests/integration/test_replay_fixture.py -v`

Expected: either PASSED (RA available) or SKIPPED with the existing reason (RA not available). Either way, no ERROR or destructure-related failure.

- [ ] **Step 1.4: Commit**

```bash
git add tests/integration/conftest.py tests/integration/test_replay_fixture.py
git commit -m "tests: replay_ra_dashboard yields SessionManager for diagnostics

Adds a 4th tuple element so the failure-report diagnostic block can
introspect session.orchestrator.poller, session.mode, etc. without
fetching internals over HTTP. test_replay_fixture.py uses an underscore
for the new element — it's there for the hook, not the test."
```

---

### Task 2: Attach harness to `run_scenario` callable

**Files:**
- Modify: `tests/integration/conftest.py` (the `run_scenario` fixture body).

- [ ] **Step 2.1: Patch the fixture**

In `tests/integration/conftest.py`, find the `run_scenario` fixture (around line 351-365):

```python
@pytest.fixture
def run_scenario(ra_harness):
    """Send a poke scenario through the RA harness and collect events."""

    async def _run(scenario_name: str, timeout: float = 30.0) -> list:
        scenario_path = SCENARIO_DIR / scenario_name
        if not scenario_path.exists():
            pytest.fail(f"Scenario file not found: {scenario_path}")
        scenario = parse_poke_file(str(scenario_path))
        return await asyncio.wait_for(
            asyncio.to_thread(ra_harness.engine.run_scenario, scenario),
            timeout=timeout,
        )

    return _run
```

Add an attribute attachment right before `return _run`:

```python
    _run.harness = ra_harness  # diagnostic hook reaches harness via funcargs["run_scenario"].harness
    return _run
```

(`_run` is a function object so `_run.harness = ...` is a legal attribute assignment.)

- [ ] **Step 2.2: Smoke-check collection**

Run: `python -m pytest --collect-only tests/integration -q 2>&1 | tail -5`

Expected: 12 integration tests collected, no errors.

- [ ] **Step 2.3: Commit**

```bash
git add tests/integration/conftest.py
git commit -m "tests: attach harness to run_scenario callable for diagnostics

Lets the failure-report hook reach the RAHarness for run_scenario-based
tests via funcargs[\"run_scenario\"].harness — closes the diagnostic
black hole on test_transitions, test_harness_isolation, and
test_retroarch_practice_smoke."
```

---

### Task 3: Refactor `_collect_diagnostics` with failing tests for two collectors

**Files:**
- Create: `tests/unit/integration/test_diagnostic_hook.py`
- Modify: `tests/integration/conftest.py` (split `_collect_diagnostics` into helpers, add harness-path branch).

- [ ] **Step 3.1: Write failing tests**

Create `tests/unit/integration/test_diagnostic_hook.py`:

```python
"""Unit tests for the integration-test diagnostic hook.

Drives `_collect_dashboard_diagnostics` and `_collect_harness_diagnostics`
with synthetic pytest.Item-shaped objects. No real RA required.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def _make_item(funcargs: dict) -> MagicMock:
    item = MagicMock()
    item.funcargs = funcargs
    return item


def test_dashboard_collector_returns_empty_when_no_fixture():
    from tests.integration.conftest import _collect_dashboard_diagnostics

    parts = _collect_dashboard_diagnostics(_make_item({}))
    assert parts == []


def test_dashboard_collector_includes_mode_and_state_version_and_poller():
    """When session is reachable via the fixture yield, the collector pulls
    session.mode, session.emu.state_version, and poller poll_count / _read_failing."""
    from tests.integration.conftest import _collect_dashboard_diagnostics

    db = MagicMock()
    db.conn.execute.return_value.fetchone.return_value = (3,)

    poller = MagicMock()
    poller.poll_count = 1234
    poller._read_failing = False

    orchestrator = MagicMock()
    orchestrator.poller = poller

    emu = MagicMock()
    emu.state_version = 42

    session = MagicMock()
    session.mode = "PRACTICE"
    session.emu = emu
    session.orchestrator = orchestrator

    item = _make_item({
        "replay_ra_dashboard": ("http://nowhere", db, MagicMock(), session)
    })
    parts = _collect_dashboard_diagnostics(item)

    joined = "\n".join(parts)
    assert "mode=PRACTICE" in joined
    assert "state_version=42" in joined
    assert "poll_count=1234" in joined
    assert "read_failing=False" in joined


def test_dashboard_collector_handles_missing_attributes_gracefully():
    """If session.orchestrator.poller doesn't expose poll_count (future change /
    different orchestrator), the collector must NOT crash — it surfaces 'n/a'
    instead, so diagnostic output still includes everything else."""
    from tests.integration.conftest import _collect_dashboard_diagnostics

    db = MagicMock()
    db.conn.execute.return_value.fetchone.return_value = (0,)

    session = MagicMock(spec=[])  # spec=[] -> no attributes; every getattr defaults
    item = _make_item({
        "replay_ra_dashboard": ("http://nowhere", db, MagicMock(), session)
    })

    # Must not raise.
    parts = _collect_dashboard_diagnostics(item)
    joined = "\n".join(parts)
    assert "mode=n/a" in joined
    assert "state_version=n/a" in joined


def test_harness_collector_returns_empty_when_no_fixture():
    from tests.integration.conftest import _collect_harness_diagnostics

    parts = _collect_harness_diagnostics(_make_item({}))
    assert parts == []


def test_harness_collector_reads_run_scenario_attached_harness():
    """When funcargs has run_scenario whose .harness attribute is set, the
    collector pulls RA process status + NCI status from it."""
    from tests.integration.conftest import _collect_harness_diagnostics
    from spinlab.retroarch.responses import StatusInfo

    proc = MagicMock()
    proc.poll.return_value = None  # alive

    client = MagicMock()
    client.port = 56789
    client.get_status.return_value = StatusInfo(state="PAUSED", frame_number=12345, game="Toothpaste.smc")

    harness = MagicMock()
    harness.proc = proc
    harness.client = client

    run_scenario_fn = MagicMock()
    run_scenario_fn.harness = harness

    item = _make_item({"run_scenario": run_scenario_fn})
    parts = _collect_harness_diagnostics(item)
    joined = "\n".join(parts)

    assert "proc_alive=True" in joined
    assert "port=56789" in joined
    assert "state='PAUSED'" in joined
    assert "frame=12345" in joined


def test_harness_collector_reads_ra_harness_fixture_directly():
    """For tests that take `ra_harness` (or `ra_harness_love_yourself`) directly
    in their signature instead of `run_scenario`, the collector reads it from
    funcargs by that name."""
    from tests.integration.conftest import _collect_harness_diagnostics
    from spinlab.retroarch.responses import StatusInfo

    proc = MagicMock()
    proc.poll.return_value = None
    client = MagicMock()
    client.port = 56790
    client.get_status.return_value = StatusInfo(state="PLAYING", frame_number=999, game="Love Yourself.smc")
    harness = MagicMock()
    harness.proc = proc
    harness.client = client

    item = _make_item({"ra_harness": harness})
    parts = _collect_harness_diagnostics(item)
    joined = "\n".join(parts)

    assert "proc_alive=True" in joined
    assert "port=56790" in joined
    assert "state='PLAYING'" in joined


def test_harness_collector_reports_dead_process():
    """If proc.poll() returns an exit code, the collector reports proc_alive=False
    AND surfaces the exit code (so an agent diagnosing the crash sees it)."""
    from tests.integration.conftest import _collect_harness_diagnostics

    proc = MagicMock()
    proc.poll.return_value = -9  # killed

    harness = MagicMock()
    harness.proc = proc
    harness.client = MagicMock()
    harness.client.port = 56791
    harness.client.get_status.side_effect = RuntimeError("RA dead, no NCI")

    item = _make_item({"ra_harness": harness})
    parts = _collect_harness_diagnostics(item)
    joined = "\n".join(parts)

    assert "proc_alive=False" in joined
    assert "exit_code=-9" in joined
    # get_status failure must not crash the collector — its error is surfaced as text
    assert "RA dead, no NCI" in joined or "<unavailable" in joined


def test_full_collect_diagnostics_includes_both_when_both_fixtures_present():
    """Belt-and-suspenders: a test that happens to depend on BOTH a dashboard
    fixture and a harness fixture gets both diagnostic blocks."""
    from tests.integration.conftest import _collect_diagnostics
    from spinlab.retroarch.responses import StatusInfo

    db = MagicMock()
    db.conn.execute.return_value.fetchone.return_value = (1,)
    session = MagicMock(spec=[])

    proc = MagicMock(); proc.poll.return_value = None
    client = MagicMock()
    client.port = 56792
    client.get_status.return_value = StatusInfo(state="PAUSED", frame_number=1, game="x.smc")
    harness = MagicMock()
    harness.proc = proc
    harness.client = client

    item = _make_item({
        "replay_ra_dashboard": ("http://nowhere", db, MagicMock(), session),
        "ra_harness": harness,
    })

    result = _collect_diagnostics(item)
    assert "SpinLab Integration Diagnostics" in result
    assert "proc_alive=True" in result
    # plus dashboard section indicators
    assert "DB:" in result or "/api/state" in result
```

- [ ] **Step 3.2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/integration/test_diagnostic_hook.py -v`
Expected: 8 tests, all ERROR with `ImportError: cannot import name '_collect_dashboard_diagnostics'` etc.

- [ ] **Step 3.3: Refactor `_collect_diagnostics` in conftest.py**

Replace the existing function body (lines 531-572 — the `def _collect_diagnostics` and its content) with the split implementation:

```python
def _collect_dashboard_diagnostics(item: pytest.Item) -> list[str]:
    """Diagnostic lines drawn from `replay_ra_dashboard` (HTTP + DB + session
    introspection). Returns [] if the fixture isn't in funcargs."""
    fixture_val = item.funcargs.get("replay_ra_dashboard")
    if fixture_val is None:
        return []

    parts: list[str] = []
    # Pre-Task 1, this was a 3-tuple. The hook is robust to either shape.
    if len(fixture_val) == 4:
        base_url, db, _tmp_path, session = fixture_val
    else:
        base_url, db, _tmp_path = fixture_val
        session = None

    # /api/state snapshot.
    try:
        state = http_requests.get(f"{base_url}/api/state", timeout=2).json()
        parts.append(f"  /api/state: {json.dumps(state, indent=2)}")
    except Exception as exc:
        parts.append(f"  /api/state: <unavailable: {exc}>")

    # DB row counts.
    try:
        seg_count = db.conn.execute(
            "SELECT COUNT(*) FROM segments WHERE active = 1"
        ).fetchone()[0]
        ref_count = db.conn.execute(
            "SELECT COUNT(*) FROM capture_runs"
        ).fetchone()[0]
        draft_count = db.conn.execute(
            "SELECT COUNT(*) FROM capture_runs WHERE draft = 1"
        ).fetchone()[0]
        parts.append(
            f"  DB: {seg_count} active segments, {ref_count} capture_runs ({draft_count} drafts)"
        )
    except Exception as exc:
        parts.append(f"  DB: <unavailable: {exc}>")

    # Session/orchestrator/poller introspection (Task 1 yields the session).
    if session is not None:
        mode = getattr(session, "mode", "n/a")
        emu = getattr(session, "emu", None)
        state_version = getattr(emu, "state_version", "n/a") if emu is not None else "n/a"
        orchestrator = getattr(session, "orchestrator", None)
        poller = getattr(orchestrator, "poller", None) if orchestrator is not None else None
        poll_count = getattr(poller, "poll_count", "n/a") if poller is not None else "n/a"
        read_failing = getattr(poller, "_read_failing", "n/a") if poller is not None else "n/a"
        parts.append(
            f"  session: mode={mode}, state_version={state_version}, "
            f"poll_count={poll_count}, read_failing={read_failing}"
        )

    return parts


def _collect_harness_diagnostics(item: pytest.Item) -> list[str]:
    """Diagnostic lines drawn from a RAHarness reachable via funcargs.

    Lookup order:
      1. `run_scenario` → its `.harness` attribute (attached in run_scenario fixture).
      2. `ra_harness` directly.
      3. `ra_harness_love_yourself` directly.

    Returns [] if none of these are in funcargs.
    """
    harness = None
    fixture_name_used = None
    run_scenario_fn = item.funcargs.get("run_scenario")
    if run_scenario_fn is not None:
        harness = getattr(run_scenario_fn, "harness", None)
        fixture_name_used = "run_scenario"
    if harness is None:
        for name in ("ra_harness", "ra_harness_love_yourself"):
            candidate = item.funcargs.get(name)
            if candidate is not None:
                harness = candidate
                fixture_name_used = name
                break
    if harness is None:
        return []

    parts: list[str] = []

    proc = getattr(harness, "proc", None)
    exit_code = proc.poll() if proc is not None else None
    proc_alive = exit_code is None
    client_port = getattr(getattr(harness, "client", None), "port", "n/a")
    line = f"  {fixture_name_used}: proc_alive={proc_alive}, port={client_port}"
    if not proc_alive:
        line += f", exit_code={exit_code}"
    parts.append(line)

    # Best-effort GET_STATUS — most common diagnostic for "did RA actually advance?".
    try:
        status = harness.client.get_status()
        parts.append(f"    RA status: state={status.state!r}, frame={status.frame_number}")
    except Exception as exc:
        parts.append(f"    RA status: <unavailable: {exc}>")

    return parts


def _collect_diagnostics(item: pytest.Item) -> str:
    """Best-effort snapshot of integration test state at failure time."""
    parts: list[str] = []
    parts.extend(_collect_dashboard_diagnostics(item))
    parts.extend(_collect_harness_diagnostics(item))

    # Recent event log (always).
    recent = _ring.recent(30)
    if recent:
        parts.append(f"  Recent log ({len(recent)} lines):")
        for line in recent:
            parts.append(f"    {line}")

    if not parts:
        return ""
    return "\n--- SpinLab Integration Diagnostics ---\n" + "\n".join(parts)
```

The `pytest_runtest_makereport` hookwrapper just below (lines 575-590) doesn't need to change — it already calls `_collect_diagnostics(item)`.

- [ ] **Step 3.4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/integration/test_diagnostic_hook.py -v`
Expected: 8 passed.

- [ ] **Step 3.5: Run the fast suite to ensure no regression**

Run: `python -m pytest -m "not emulator"`
Expected: All pass.

- [ ] **Step 3.6: Commit**

```bash
git add tests/integration/conftest.py tests/unit/integration/test_diagnostic_hook.py
git commit -m "tests: split _collect_diagnostics + cover run_scenario tests

Splits into _collect_dashboard_diagnostics + _collect_harness_diagnostics
so failing run_scenario tests (test_transitions et al.) now get a
diagnostic block too. Adds: session.mode, state_version, poller
poll_count and _read_failing, harness proc liveness/exit code, RA NCI
status. All fields use getattr with 'n/a' fallback so future production
changes don't break the hook."
```

---

### Task 4: End-to-end verification

**Files:** none (verification only).

- [ ] **Step 4.1: Run full pytest**

Run: `python -m pytest`
Expected: 866 passed. Specifically: emulator tests either all pass or surface real failures (if C1-C3 has landed) / continue to skip cleanly (if C1-C3 has not landed — this plan is independent).

- [ ] **Step 4.2: Synthetic failure check (optional confidence-build)**

Pick one passing integration test and temporarily inject `assert False` to confirm the diagnostic block fires with the expected new fields. Then revert.

Example: in `tests/integration/test_transitions.py::test_entrance_goal`, add `assert False, "smoke"` after the scenario runs. Run the test. The pytest output should include something like:

```
--- SpinLab Integration Diagnostics ---
  run_scenario: proc_alive=True, port=55001
    RA status: state='PAUSED', frame=1234
  Recent log (30 lines):
    ...
```

Revert the synthetic assert before committing anything.

- [ ] **Step 4.3: No commit (verification only)**

If everything looks good, this task is done. If the synthetic failure check revealed a bug in the collectors, return to Task 3 and patch.

---

## Self-Review

**Spec coverage:**
- ✅ D1 (hook only fires for one fixture) — Task 3's split into two collectors dispatches on whichever fixture is in funcargs.
- ✅ D2 (missing fields) — Task 3 adds session.mode, state_version, poll_count, _read_failing, RA proc liveness + exit code, RA NCI state + frame number.
- ✅ Tests harness — Task 3 adds 8 unit tests using synthetic Items.
- ✅ Backward compatibility for `replay_ra_dashboard` callers — only `test_replay_fixture.py` destructures; updated in Task 1.
- ✅ Future-proof getattr defaults — Task 3 tests `test_dashboard_collector_handles_missing_attributes_gracefully`.

**Placeholder scan:** No "TBD" / "implement later" placeholders. Code blocks are complete.

**Type consistency:** `_collect_dashboard_diagnostics(item: pytest.Item) -> list[str]`, `_collect_harness_diagnostics(item: pytest.Item) -> list[str]`, `_collect_diagnostics(item: pytest.Item) -> str`. Consistent across tasks.

**Fields NOT included (and why):**
- NCI consecutive-timeout count → not yet instrumented on `NCIClient`; deferred (would be a separate plan).
- Cold-fill queue state → only meaningful when ColdFillController is mid-batch; rare in current emulator tests; the introspection path (session.cold_fill.<something>) would need verification. Defer.
- Practice/speedrun timing state → similar to cold-fill; not exercised by current emulator tests. Defer.

If you find one of those becomes the critical piece while debugging a real failure, add it then — `getattr` defaults mean adding a field is one line in `_collect_dashboard_diagnostics`.
