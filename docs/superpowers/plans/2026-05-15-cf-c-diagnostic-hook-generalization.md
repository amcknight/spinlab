# CF-C — Integration-Test Diagnostic Hook Generalization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the next flaky integration-test failure debuggable. Today a full-suite `python -m pytest` can fail intermittently (NCITimeout, IndexError) and we get no actionable context — the diagnostic hook only fires for `replay_ra_dashboard`, RA's stdout/stderr is `DEVNULL`'d, and the harness factory wraps `RAHarnessLaunchError` as a context-less `RuntimeError`.

**Architecture:** Six small commits, scoped strictly to `tests/integration/` (no `python/spinlab/` source changes). Each adds one diagnostic surface: per-launch RA logfiles, structured `RAHarnessLaunchError`, factory error propagation, generalized funcarg-walking diagnostics, RA-process-state in the diagnostic block, and richer fail messages on the two known swallowed paths.

**Tech Stack:** Python 3.11+, pytest, pytest-asyncio, subprocess. No new dependencies. Existing unit-test pattern uses `MagicMock` + `tmp_path` (see `tests/unit/integration/test_ra_harness.py`).

---

## File Structure

**Modified files:**
- `tests/integration/ra_harness.py` — stdout/stderr → per-launch logfile, structured `RAHarnessLaunchError` fields, log path accessor for diagnostics
- `tests/integration/conftest.py:358-360` — stop wrapping `RAHarnessLaunchError` as `RuntimeError`
- `tests/integration/conftest.py:282` — dashboard timeout message gets port + last health-check error
- `tests/integration/conftest.py:567-576` — pause_toggle exception re-raised with context
- `tests/integration/conftest.py:619-665` — `_collect_diagnostics` iterates `item.funcargs` generically, adds RA `.proc.poll()` + stderr tail

**Test files:**
- `tests/unit/integration/test_ra_harness.py` — new tests for logfile creation, error fields
- `tests/unit/integration/test_diagnostic_hook.py` — **new file** — covers `_collect_diagnostics` shape detection (no RA needed; uses mock harness objects)

Each task is one commit. TDD throughout: failing test, minimal implementation, passing test, commit.

---

### Task 1: Per-launch RA logfile (stdout + stderr → disk)

Right now `RAHarness.launch` passes `stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL` (line 181-182). When RA crashes or panics, the operator sees an `NCITimeout` with no further information. Redirect both streams to a single combined logfile under the harness's per-launch `tmp_dir`. The path is stored on `RAHarness` so the diagnostic hook can read the tail.

**Files:**
- Modify: `tests/integration/ra_harness.py:64-72` (add `_log_path` field), `:179-183` (redirect to file)
- Modify: `tests/integration/ra_harness.py:281-293` (close log file handle on teardown)
- Test: `tests/unit/integration/test_ra_harness.py` (new test)

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/integration/test_ra_harness.py`:

```python
def test_launch_writes_ra_stdout_stderr_to_logfile(fake_paths, fake_proc):
    """Launch must point RA's stdout/stderr at a real file we can inspect later."""
    rom, core, exe = fake_paths

    captured: dict[str, object] = {}

    def fake_popen(cmd, **kwargs):
        captured["stdout"] = kwargs.get("stdout")
        captured["stderr"] = kwargs.get("stderr")
        return fake_proc

    fake_client = MagicMock()
    fake_client.version.return_value = None
    status = MagicMock()
    status.state = "PAUSED"
    fake_client.get_status.return_value = status

    with patch("tests.integration.ra_harness.subprocess.Popen", side_effect=fake_popen), \
         patch("tests.integration.ra_harness.NCIClient", return_value=fake_client):
        harness = RAHarness.launch(rom_path=rom, core_path=core, retroarch_exe=exe)

    # stdout and stderr should be open file handles pointing at the same file.
    assert captured["stdout"] is not None
    assert captured["stderr"] is not None
    assert captured["stdout"] is captured["stderr"], "stderr must alias stdout for combined log"

    # The harness must expose the log path so the diagnostic hook can tail it.
    assert harness.log_path is not None
    assert harness.log_path.exists()
    assert harness.log_path.parent == harness._tmp_dir
    harness.teardown()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/integration/test_ra_harness.py::test_launch_writes_ra_stdout_stderr_to_logfile -v`
Expected: FAIL with `AttributeError: 'RAHarness' object has no attribute 'log_path'` (or `DEVNULL` assertion failure).

- [ ] **Step 3: Implement — add `log_path` field and redirect Popen streams**

In `tests/integration/ra_harness.py`, modify the dataclass (lines 63-72):

```python
@dataclass
class RAHarness:
    proc: subprocess.Popen
    client: NCIClient
    log_path: Path | None = field(default=None, repr=False)
    _log_handle: object = field(default=None, repr=False)
    _tmp_dir: Path | None = field(default=None, repr=False)
    fresh_boot_slot: int | None = field(default=None, repr=False)
    engine: RAPokeEngine = field(init=False)

    def __post_init__(self) -> None:
        self.engine = RAPokeEngine(self.client, fresh_boot_slot=self.fresh_boot_slot)
```

In `RAHarness.launch`, replace the `subprocess.Popen(cmd, stdout=DEVNULL, stderr=DEVNULL)` block (current lines 178-183) with:

```python
        logger.info("ra_harness: launching %s on NCI port %d", cmd, port)
        log_path = tmp_dir / "retroarch.log"
        log_handle = open(log_path, "wb")
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=log_handle,
                stderr=log_handle,  # combined log; stderr aliases stdout
            )
        except Exception:
            log_handle.close()
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise
```

At the bottom of `launch` (current line 278-279), replace:

```python
        slot = FRESH_BOOT_STATE_SLOT if fresh_state_path is not None else None
        return cls(proc=proc, client=client, _tmp_dir=tmp_dir, fresh_boot_slot=slot)
```

with:

```python
        slot = FRESH_BOOT_STATE_SLOT if fresh_state_path is not None else None
        return cls(
            proc=proc,
            client=client,
            log_path=log_path,
            _log_handle=log_handle,
            _tmp_dir=tmp_dir,
            fresh_boot_slot=slot,
        )
```

Every existing failure path inside `launch` that calls `cls._kill(proc)` + `shutil.rmtree(tmp_dir, ...)` must also close the log handle. Add a small helper near `_kill` (around line 295) and call it from each failure branch:

```python
    @staticmethod
    def _cleanup_launch(proc: subprocess.Popen, log_handle, tmp_dir: Path) -> None:
        """Tear down a half-launched harness on a launch-failure path."""
        RAHarness._kill(proc)
        try:
            log_handle.close()
        except Exception:
            pass
        shutil.rmtree(tmp_dir, ignore_errors=True)
```

Then in each `cls._kill(proc)` + `shutil.rmtree(...)` pair inside `launch` (current lines 194-198, 218-220, 252-262, 264-268), replace with:

```python
            cls._cleanup_launch(proc, log_handle, tmp_dir)
            raise RAHarnessLaunchError(...)  # message unchanged for now
```

Update `teardown` (currently lines 281-293) to close the log handle:

```python
    def teardown(self) -> None:
        try:
            self.client.quit()
        except Exception as exc:
            logger.warning("ra_harness: client.quit() raised %s", exc)
        try:
            self.proc.wait(timeout=QUIT_GRACE_S)
        except subprocess.TimeoutExpired:
            self._kill(self.proc)
        self.client.close()
        if self._log_handle is not None:
            try:
                self._log_handle.close()
            except Exception:
                pass
            self._log_handle = None
        if self._tmp_dir is not None:
            shutil.rmtree(self._tmp_dir, ignore_errors=True)
            self._tmp_dir = None
```

Also remove the now-unused `subprocess.DEVNULL` references (they may not exist anymore but verify the import line stays correct).

- [ ] **Step 4: Run test to verify it passes + full unit suite still green**

Run: `python -m pytest tests/unit/integration/test_ra_harness.py -v`
Expected: All tests PASS including the new one.

Run: `python -m pytest -m "not emulator"`
Expected: 865+ pass, 0 fail.

- [ ] **Step 5: Smoke-test the emulator suite**

Run: `python -m pytest tests/integration/test_retroarch_practice_smoke.py -v`
Expected: PASS. Visually confirm a `retroarch.log` file is created (peek the temp dir during the run if uncertain).

- [ ] **Step 6: Commit**

```bash
git add tests/integration/ra_harness.py tests/unit/integration/test_ra_harness.py
git commit -m "ra_harness: capture RA stdout/stderr to per-launch logfile (CF-C step 1)"
```

---

### Task 2: Structured `RAHarnessLaunchError` fields

Today every failure path inside `RAHarness.launch` raises `RAHarnessLaunchError(<f-string>)` — the only context is whatever the string included. When the factory wraps this as `RuntimeError`, even the original exception type is gone. Add structured fields the diagnostic hook can read programmatically.

**Files:**
- Modify: `tests/integration/ra_harness.py:59-61` (extend exception class)
- Modify: every `raise RAHarnessLaunchError(...)` inside `launch` (currently 6 sites: lines 109, 146, 196, 220, 256, 258, 266)
- Test: `tests/unit/integration/test_ra_harness.py` (new test)

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/integration/test_ra_harness.py`:

```python
def test_launch_error_carries_context_fields(fake_paths, fake_proc):
    """When NCI never replies, the raised error carries pid + port + duration."""
    rom, core, exe = fake_paths

    fake_client = MagicMock()
    fake_client.version.side_effect = NCITimeout("no reply")

    with patch("tests.integration.ra_harness.subprocess.Popen", return_value=fake_proc), \
         patch("tests.integration.ra_harness.NCIClient", return_value=fake_client), \
         patch("tests.integration.ra_harness.time.sleep"), \
         pytest.raises(RAHarnessLaunchError) as excinfo:
        RAHarness.launch(rom_path=rom, core_path=core, retroarch_exe=exe, nci_port=55355)

    err = excinfo.value
    assert err.pid == fake_proc.pid
    assert err.port == 55355
    assert err.stage == "nci_ping"
    assert err.startup_duration_s is not None and err.startup_duration_s >= 0.0
    assert err.log_path is not None and err.log_path.exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/integration/test_ra_harness.py::test_launch_error_carries_context_fields -v`
Expected: FAIL with `AttributeError: 'RAHarnessLaunchError' object has no attribute 'pid'`.

- [ ] **Step 3: Implement — extend the exception, thread context through launch**

In `tests/integration/ra_harness.py`, replace the bare exception class (lines 59-61):

```python
class RAHarnessLaunchError(RuntimeError):
    """Raised when RA fails to launch into a usable state.

    Carries structured context so the failure can be reported without parsing
    the message string. ``pid``/``port``/``startup_duration_s`` are populated
    once Popen has succeeded; before that they stay ``None``. ``log_path``
    points at the per-launch RA log so the caller can tail it for context.
    """

    def __init__(
        self,
        message: str,
        *,
        stage: str,
        pid: int | None = None,
        port: int | None = None,
        startup_duration_s: float | None = None,
        log_path: Path | None = None,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.pid = pid
        self.port = port
        self.startup_duration_s = startup_duration_s
        self.log_path = log_path
```

Near the top of `RAHarness.launch`, capture a start timestamp right after the path-existence checks (around line 110):

```python
        launch_started_at = time.monotonic()
```

Each existing `raise RAHarnessLaunchError(message)` site inside `launch` becomes a `raise RAHarnessLaunchError(message, stage=..., ...)` call. Specifically:

Line 109 (path-existence failure — before Popen runs, no pid/port):
```python
                raise RAHarnessLaunchError(
                    f"{label} does not exist: {p}", stage="path_check"
                )
```

Line 146 (fresh_state_path missing):
```python
                raise RAHarnessLaunchError(
                    f"fresh_state_path does not exist: {fresh_state_path}",
                    stage="fresh_state_path_check",
                )
```

Line 196-198 (NCI never replied):
```python
            cls._cleanup_launch(proc, log_handle, tmp_dir)
            raise RAHarnessLaunchError(
                f"NCI did not reply after {NCI_PING_RETRIES} attempts × {NCI_PING_INTERVAL_S}s",
                stage="nci_ping",
                pid=proc.pid,
                port=port,
                startup_duration_s=time.monotonic() - launch_started_at,
                log_path=log_path,
            )
```

Line 220 (GET_STATUS failed):
```python
            cls._cleanup_launch(proc, log_handle, tmp_dir)
            raise RAHarnessLaunchError(
                f"GET_STATUS failed: {exc}",
                stage="get_status",
                pid=proc.pid,
                port=port,
                startup_duration_s=time.monotonic() - launch_started_at,
                log_path=log_path,
            ) from exc
```

Lines 254-262 (PAUSE_TOGGLE didn't take):
```python
                cls._cleanup_launch(proc, log_handle, tmp_dir)
                if last_exc is not None and after_state is None:
                    raise RAHarnessLaunchError(
                        f"GET_STATUS after pause_toggle kept failing: {last_exc}",
                        stage="pause_verify",
                        pid=proc.pid,
                        port=port,
                        startup_duration_s=time.monotonic() - launch_started_at,
                        log_path=log_path,
                    ) from last_exc
                raise RAHarnessLaunchError(
                    f"PAUSE_TOGGLE did not pause RA after "
                    f"{PAUSE_VERIFY_RETRIES} retries "
                    f"(last status={after_state!r})",
                    stage="pause_verify",
                    pid=proc.pid,
                    port=port,
                    startup_duration_s=time.monotonic() - launch_started_at,
                    log_path=log_path,
                )
```

Lines 264-268 (unexpected status):
```python
            cls._cleanup_launch(proc, log_handle, tmp_dir)
            raise RAHarnessLaunchError(
                f"Unexpected RA status after launch: {status.state!r} — expected PAUSED or PLAYING",
                stage="status_unexpected",
                pid=proc.pid,
                port=port,
                startup_duration_s=time.monotonic() - launch_started_at,
                log_path=log_path,
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/integration/test_ra_harness.py -v`
Expected: All tests PASS, including the new field test.

- [ ] **Step 5: Verify call sites still build**

Run: `python -m pytest -m "not emulator"` + `python -m pytest tests/integration/test_retroarch_practice_smoke.py`
Expected: All green. (The factory at conftest.py:358-360 still wraps as RuntimeError — that's Task 3.)

- [ ] **Step 6: Commit**

```bash
git add tests/integration/ra_harness.py tests/unit/integration/test_ra_harness.py
git commit -m "ra_harness: structured RAHarnessLaunchError with pid/port/stage/log_path (CF-C step 2)"
```

---

### Task 3: Stop wrapping `RAHarnessLaunchError` as bare `RuntimeError`

The factory at `tests/integration/conftest.py:358-360` re-raises every `RAHarnessLaunchError` as `RuntimeError`, losing the typed exception and its new structured fields. Propagate the original instead, with the `rom_key` annotated.

**Files:**
- Modify: `tests/integration/conftest.py:358-360`
- Test: `tests/unit/integration/test_ra_harness_factory.py` (new test)

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/integration/test_ra_harness_factory.py`:

```python
def test_factory_propagates_ra_harness_launch_error(monkeypatch):
    """Factory must not swallow RAHarnessLaunchError as bare RuntimeError —
    structured fields and the typed class are needed by the diagnostic hook."""
    from tests.integration.conftest import _HarnessFactory
    from tests.integration.ra_harness import RAHarnessLaunchError

    def fake_launch(**kwargs):
        raise RAHarnessLaunchError(
            "fake failure", stage="nci_ping", pid=12345, port=55355
        )

    monkeypatch.setattr(
        "tests.integration.conftest._resolve_ra_paths",
        lambda rom_key: (Path("ra.exe"), Path("core.dll"), Path("rom.smc")),
    )
    monkeypatch.setattr(
        "tests.integration.conftest._state_path_for",
        lambda rom_basename: None,
    )
    monkeypatch.setattr(
        "tests.integration.conftest._free_udp_port", lambda: 55355
    )
    monkeypatch.setattr(
        "tests.integration.conftest.RAHarness.launch", staticmethod(fake_launch)
    )

    factory = _HarnessFactory()
    with pytest.raises(RAHarnessLaunchError) as excinfo:
        factory("love_yourself", use_fresh_state=False)
    assert excinfo.value.pid == 12345
    assert excinfo.value.stage == "nci_ping"
    assert "love_yourself" in str(excinfo.value)
```

(Import `Path` and `pytest` if not already imported in that file.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/integration/test_ra_harness_factory.py::test_factory_propagates_ra_harness_launch_error -v`
Expected: FAIL — `pytest.raises(RAHarnessLaunchError)` won't match because the factory raises `RuntimeError`.

- [ ] **Step 3: Implement — propagate the typed exception**

In `tests/integration/conftest.py`, replace lines 358-360:

```python
        except RAHarnessLaunchError as exc:
            # CLAUDE.md: launch failure is a FAILURE, not a skip.
            raise RuntimeError(f"ra_harness launch failed for rom_key={rom_key!r}: {exc}") from exc
```

with:

```python
        except RAHarnessLaunchError as exc:
            # CLAUDE.md: launch failure is a FAILURE, not a skip. Re-raise the
            # typed exception so the diagnostic hook can read its structured
            # fields (pid, port, stage, log_path). Annotate args with rom_key
            # so the test report still names the harness that failed.
            exc.args = (f"ra_harness launch failed for rom_key={rom_key!r}: {exc.args[0]}",)
            raise
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/integration/test_ra_harness_factory.py -v`
Expected: All tests PASS.

- [ ] **Step 5: Verify the broader unit suite is unaffected**

Run: `python -m pytest -m "not emulator"`
Expected: All 865+ green. (CLAUDE.md "hard-fail not skip" rule still satisfied because `RAHarnessLaunchError` is itself a `RuntimeError` subclass.)

- [ ] **Step 6: Commit**

```bash
git add tests/integration/conftest.py tests/unit/integration/test_ra_harness_factory.py
git commit -m "conftest: factory propagates typed RAHarnessLaunchError, not bare RuntimeError (CF-C step 3)"
```

---

### Task 4: Generalize `_collect_diagnostics` to walk all funcargs

Today the diagnostic hook hard-codes one fixture (`tests/integration/conftest.py:629`):

```python
for fixture_name in ("replay_ra_dashboard",):
    fixture_val = item.funcargs.get(fixture_name)
```

That misses every `run_scenario` test (the bulk of transition coverage). Replace with a duck-typed scan: walk `item.funcargs`, detect dashboard-shaped tuples and harness-shaped objects, emit a block for each.

**Files:**
- Modify: `tests/integration/conftest.py:624-665` (`_collect_diagnostics`)
- Test: `tests/unit/integration/test_diagnostic_hook.py` (**new file**)

- [ ] **Step 1: Write the failing test**

Create `tests/unit/integration/test_diagnostic_hook.py`:

```python
"""Tests for the integration-test diagnostic hook (_collect_diagnostics).

These run as unit tests because they need no live RA — mock funcargs with the
shapes our integration fixtures yield.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from tests.integration.conftest import _collect_diagnostics


@pytest.fixture
def mock_item():
    item = MagicMock()
    item.funcargs = {}
    return item


def test_collect_diagnostics_emits_block_for_dashboard_tuple(mock_item, monkeypatch):
    """A funcarg yielding (base_url, db, _) gets the /api/state + DB block."""
    db = MagicMock()
    db.conn.execute.return_value.fetchone.return_value = (5,)
    mock_item.funcargs["replay_ra_dashboard"] = ("http://x:1", db, None)

    fake_resp = MagicMock()
    fake_resp.json.return_value = {"emu_connected": True}
    monkeypatch.setattr(
        "tests.integration.conftest.http_requests.get",
        lambda url, timeout=2: fake_resp,
    )

    out = _collect_diagnostics(mock_item)
    assert "/api/state" in out
    assert "emu_connected" in out
    assert "active segments" in out


def test_collect_diagnostics_emits_block_for_harness_funcarg(mock_item):
    """A funcarg duck-typing as a harness (.proc, .client, .log_path) gets
    a process-state + log-tail block, even if the fixture isn't the dashboard."""
    harness = MagicMock()
    harness.proc.poll.return_value = None  # still alive
    harness.proc.pid = 4242
    harness.client.port = 55355
    harness.log_path = MagicMock()
    harness.log_path.exists.return_value = True
    harness.log_path.read_text.return_value = "\n".join(f"line {i}" for i in range(50))

    mock_item.funcargs["ra_harness_love_yourself"] = harness
    out = _collect_diagnostics(mock_item)

    assert "harness: ra_harness_love_yourself" in out
    assert "pid=4242" in out
    assert "port=55355" in out
    assert "proc.poll()=None" in out  # still alive
    # Should include only the tail, not all 50 lines
    assert "line 49" in out
    assert "line 19" not in out  # well before the tail


def test_collect_diagnostics_reports_dead_ra_process(mock_item):
    """If proc.poll() returns a non-None exit code, that surfaces in the block."""
    harness = MagicMock()
    harness.proc.poll.return_value = -11  # SIGSEGV-equivalent
    harness.proc.pid = 4242
    harness.client.port = 55355
    harness.log_path = None  # no log path is OK

    mock_item.funcargs["ra_harness_love_yourself"] = harness
    out = _collect_diagnostics(mock_item)
    assert "proc.poll()=-11" in out


def test_collect_diagnostics_returns_empty_when_no_funcargs_match(mock_item):
    """If a test has no integration funcargs, the diagnostic block is empty."""
    mock_item.funcargs["unrelated_fixture"] = MagicMock(spec=[])
    out = _collect_diagnostics(mock_item)
    # No harness, no dashboard, no log lines — empty block.
    # (Ring buffer may add log lines from earlier session activity; this test
    # only asserts the dashboard/harness sections are absent.)
    assert "/api/state" not in out
    assert "harness:" not in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/integration/test_diagnostic_hook.py -v`
Expected: FAIL — current `_collect_diagnostics` ignores anything that isn't `replay_ra_dashboard`, has no harness section.

- [ ] **Step 3: Implement — rewrite `_collect_diagnostics` to walk funcargs**

In `tests/integration/conftest.py`, replace the entire `_collect_diagnostics` function (current lines 624-665) with:

```python
_HARNESS_LOG_TAIL_LINES = 30  # last N lines of retroarch.log on failure
_RING_TAIL_LINES = 30  # last N spinlab log lines on failure


def _collect_diagnostics(item: pytest.Item) -> str:
    """Best-effort snapshot of integration test state at failure time.

    Walks ``item.funcargs`` and:
      - For tuples shaped ``(str_url, Database, ...)``, emits an /api/state +
        DB-counts block (matches the dashboard-fixture shape).
      - For objects exposing ``.proc`` (a Popen) and ``.client`` (NCIClient),
        emits a harness block with pid / port / proc.poll() and the last
        ``_HARNESS_LOG_TAIL_LINES`` of the per-launch retroarch.log if available.

    Always tails the in-process spinlab log ring buffer at the end.
    """
    parts: list[str] = []

    for fixture_name, fixture_val in item.funcargs.items():
        # ---- Dashboard-shaped: (base_url, db, _) ----
        if (
            isinstance(fixture_val, tuple)
            and len(fixture_val) >= 2
            and isinstance(fixture_val[0], str)
            and fixture_val[0].startswith("http")
        ):
            base_url = fixture_val[0]
            db = fixture_val[1]
            parts.append(f"  fixture: {fixture_name}")
            try:
                state = http_requests.get(f"{base_url}/api/state", timeout=2).json()
                parts.append(f"  /api/state: {json.dumps(state, indent=2)}")
            except Exception as exc:
                parts.append(f"  /api/state: <unavailable: {exc}>")
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
                    f"  DB: {seg_count} active segments, "
                    f"{ref_count} capture_runs ({draft_count} drafts)"
                )
            except Exception as exc:
                parts.append(f"  DB: <unavailable: {exc}>")
            continue

        # ---- Harness-shaped: duck-types on .proc + .client ----
        if hasattr(fixture_val, "proc") and hasattr(fixture_val, "client"):
            try:
                proc_status = fixture_val.proc.poll()
            except Exception as exc:
                proc_status = f"<poll failed: {exc}>"
            try:
                port = fixture_val.client.port
            except Exception:
                port = "<unknown>"
            try:
                pid = fixture_val.proc.pid
            except Exception:
                pid = "<unknown>"
            parts.append(
                f"  harness: {fixture_name} pid={pid} port={port} proc.poll()={proc_status}"
            )
            log_path = getattr(fixture_val, "log_path", None)
            if log_path is not None:
                try:
                    if log_path.exists():
                        text = log_path.read_text(errors="replace")
                        tail = text.splitlines()[-_HARNESS_LOG_TAIL_LINES:]
                        if tail:
                            parts.append(f"  retroarch.log tail ({len(tail)} lines):")
                            for line in tail:
                                parts.append(f"    {line}")
                except Exception as exc:
                    parts.append(f"  retroarch.log: <unavailable: {exc}>")

    # --- Recent event log (always include if anything in the ring) ---
    recent = _ring.recent(_RING_TAIL_LINES)
    if recent:
        parts.append(f"  Recent spinlab log ({len(recent)} lines):")
        for line in recent:
            parts.append(f"    {line}")

    if not parts:
        return ""
    return "\n--- SpinLab Integration Diagnostics ---\n" + "\n".join(parts)
```

(`pytest`, `http_requests`, `json`, and `_ring` are already imported at the top of conftest.py — confirm before relying.)

- [ ] **Step 4: Run new tests + verify unit suite**

Run: `python -m pytest tests/unit/integration/test_diagnostic_hook.py -v`
Expected: All 4 new tests PASS.

Run: `python -m pytest -m "not emulator"`
Expected: All green.

- [ ] **Step 5: Emulator smoke**

Run: `python -m pytest tests/integration/test_transitions.py::test_entrance_goal -v`
Expected: PASS. (The diagnostic surface is only exercised on failure; we just want to confirm we didn't break the import chain.)

- [ ] **Step 6: Commit**

```bash
git add tests/integration/conftest.py tests/unit/integration/test_diagnostic_hook.py
git commit -m "conftest: generalize _collect_diagnostics to walk funcargs (CF-C step 4)"
```

---

### Task 5: Re-raise `pause_toggle` failure with context

`tests/integration/conftest.py:567-576` catches an exception during the pre-replay `pause_toggle`, logs a warning, and yields anyway. Downstream the test fails with an unrelated-looking "core not paused" message and zero direct trace to the swallowed call. Re-raise with full context so the diagnostic block kicks in instead.

**Files:**
- Modify: `tests/integration/conftest.py:567-576`
- Test: `tests/unit/integration/test_diagnostic_hook.py` (extend; no live RA needed)

- [ ] **Step 1: Decide what behavior is right + write a test for the message**

The pause_toggle is a precondition for the replay flow. If RA refuses to unpause, the test cannot succeed — pretending otherwise leaks failure into the test body. Replace `warning + yield` with `pytest.fail` carrying the harness pid/port and the original exception.

Test (append to `tests/unit/integration/test_diagnostic_hook.py`):

```python
def test_pause_toggle_failure_message_includes_context():
    """Sanity check on the format helper used in the fixture path. Verifies
    the helper exists and produces a message that names the harness, the
    underlying exception, and the harness port/pid."""
    from tests.integration.conftest import _format_pause_toggle_failure

    harness = MagicMock()
    harness.proc.pid = 4242
    harness.client.port = 55355
    msg = _format_pause_toggle_failure(harness, RuntimeError("nci unresponsive"))
    assert "4242" in msg
    assert "55355" in msg
    assert "nci unresponsive" in msg
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/integration/test_diagnostic_hook.py::test_pause_toggle_failure_message_includes_context -v`
Expected: FAIL — `_format_pause_toggle_failure` doesn't exist yet.

- [ ] **Step 3: Implement — add helper, swap in the fail call**

In `tests/integration/conftest.py`, add a helper near the existing `_collect_diagnostics` (e.g. immediately before it, around the current line 624):

```python
def _format_pause_toggle_failure(harness, exc: Exception) -> str:
    """Format a pause_toggle failure message for the replay fixture path.

    Pulls pid/port off the harness defensively (older test doubles may not
    have them) and includes the original exception type + message.
    """
    try:
        pid = harness.proc.pid
    except Exception:
        pid = "<unknown>"
    try:
        port = harness.client.port
    except Exception:
        port = "<unknown>"
    return (
        f"replay_ra_dashboard: pause_toggle on harness "
        f"(pid={pid}, port={port}) failed with {type(exc).__name__}: {exc}"
    )
```

Replace the body of the existing try/except at lines 567-576:

```python
    harness = ra_harness_love_yourself_no_reset
    try:
        status = harness.client.get_status()
        if status.state == "PAUSED":
            harness.client.pause_toggle()
            _time.sleep(0.3)  # allow RA to settle into PLAYING before the test POSTs replay/start
    except Exception as exc:
        # Tear down what we built before failing — preserves the no-yield
        # invariant for downstream cleanup hooks.
        server.should_exit = True
        thread.join(timeout=5)
        db.close()
        import shutil as _s
        _s.rmtree(tmp, ignore_errors=True)
        pytest.fail(_format_pause_toggle_failure(harness, exc))
```

- [ ] **Step 4: Run new test + unit suite**

Run: `python -m pytest tests/unit/integration/test_diagnostic_hook.py -v`
Expected: All tests PASS.

Run: `python -m pytest -m "not emulator"`
Expected: All green.

- [ ] **Step 5: Smoke-test the replay fixture**

Run: `python -m pytest tests/integration/test_replay_fixture.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add tests/integration/conftest.py tests/unit/integration/test_diagnostic_hook.py
git commit -m "conftest: re-raise pause_toggle failure with harness context (CF-C step 5)"
```

---

### Task 6: Flesh out dashboard startup-timeout message

`tests/integration/conftest.py:282` currently says `"Fake dashboard server did not start within 10 seconds"`. Add the bound port and the last HTTP error so the operator can see whether the port was occupied or the dashboard panicked.

**Files:**
- Modify: `tests/integration/conftest.py:271-290` (or wherever the retry loop is — line numbers shift after Task 5)
- Test: `tests/unit/integration/test_diagnostic_hook.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/integration/test_diagnostic_hook.py`:

```python
def test_dashboard_startup_timeout_message_includes_port_and_error():
    """The retry loop's failure helper names the port it tried and the
    most recent connection error."""
    from tests.integration.conftest import _format_dashboard_startup_failure

    msg = _format_dashboard_startup_failure(
        port=18080,
        attempts=40,
        interval_s=0.25,
        last_error=ConnectionError("port not listening"),
    )
    assert "18080" in msg
    assert "10.0" in msg  # 40 × 0.25 = 10.0
    assert "port not listening" in msg
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/integration/test_diagnostic_hook.py::test_dashboard_startup_timeout_message_includes_port_and_error -v`
Expected: FAIL — `_format_dashboard_startup_failure` doesn't exist.

- [ ] **Step 3: Implement — add helper, capture last error in the retry loop**

In `tests/integration/conftest.py`, add the helper next to `_format_pause_toggle_failure`:

```python
def _format_dashboard_startup_failure(
    *, port: int, attempts: int, interval_s: float, last_error: Exception | None
) -> str:
    """Format the fake_dashboard_server retry-loop timeout message."""
    elapsed = attempts * interval_s
    err_str = (
        f"{type(last_error).__name__}: {last_error}" if last_error else "no error captured"
    )
    return (
        f"Fake dashboard server did not start on port {port} within "
        f"{elapsed:.1f}s ({attempts} × {interval_s}s). Last error: {err_str}"
    )
```

Then modify the retry loop in `fake_dashboard_server` (current lines 273-282):

```python
    base_url = f"http://127.0.0.1:{dashboard_port}"
    last_error: Exception | None = None
    for _ in range(40):
        try:
            resp = http_requests.get(f"{base_url}/api/state", timeout=1)
            if resp.status_code == 200:
                break
            last_error = http_requests.HTTPError(f"status {resp.status_code}")
        except http_requests.ConnectionError as exc:
            last_error = exc
        except Exception as exc:
            last_error = exc
        await asyncio.sleep(0.25)
    else:
        pytest.fail(_format_dashboard_startup_failure(
            port=dashboard_port,
            attempts=40,
            interval_s=0.25,
            last_error=last_error,
        ))
```

Apply the same shape to the **second** retry loop further down at current lines 547-561 (orchestrator-doesn't-connect):

```python
    last_state_error: Exception | None = None
    for _ in range(40):
        try:
            resp = http_requests.get(f"{base_url}/api/state", timeout=2)
            state = resp.json()
            if state.get("emu_connected") and state.get("game_id"):
                break
            last_state_error = RuntimeError(
                f"emu_connected={state.get('emu_connected')!r} "
                f"game_id={state.get('game_id')!r}"
            )
        except Exception as exc:
            last_state_error = exc
        _time.sleep(0.25)
    else:
        server.should_exit = True
        thread.join(timeout=5)
        db.close()
        import shutil as _s
        _s.rmtree(tmp, ignore_errors=True)
        pytest.fail(_format_dashboard_startup_failure(
            port=dashboard_port,
            attempts=40,
            interval_s=0.25,
            last_error=last_state_error,
        ))
```

- [ ] **Step 4: Run new test + unit suite**

Run: `python -m pytest tests/unit/integration/test_diagnostic_hook.py -v`
Expected: All tests PASS.

Run: `python -m pytest -m "not emulator"`
Expected: All green.

- [ ] **Step 5: Full-suite verification**

Run: `python -m pytest`
Expected: 877 pass. (Re-run once if the known order-flake bites — see scan file. If it still fails after 2 runs, the new diagnostic block should now make the cause visible; surface that to Andrew rather than retrying further.)

- [ ] **Step 6: Commit**

```bash
git add tests/integration/conftest.py tests/unit/integration/test_diagnostic_hook.py
git commit -m "conftest: dashboard timeout messages include port + last error (CF-C step 6)"
```

---

## After all tasks land

- [ ] **Final full-suite verification:** `python -m pytest`. Must be green per CLAUDE.md merge rule. Re-run once if a known intermittent flake hits — the goal of this entire plan is to make the *next* flake debuggable, but a green suite is still required to ship.
- [ ] **Update the scan file:** in `docs/superpowers/scans/2026-05-15-improve.md`, move CF-C from the **Picked this session** list note (just plan-written) to a status confirming all 6 tasks landed; leave the scan file's findings list untouched.
- [ ] **Memory update:** add a brief project memory note that CF-C carry-over is closed. Slug: `project_cf_c_diagnostic_landed`. Reference [[project_post_migration_audit_2026_05_10]] and [[project_encapsulation_pass_2026_05_11]].

---

## Self-review

**Spec coverage check:**
- Per-launch RA logfile → Task 1 ✓
- Generalize `_collect_diagnostics` beyond `replay_ra_dashboard` → Task 4 ✓
- Detect harness-bearing fixtures via duck-typing (`.proc`/`.client`) → Task 4 ✓
- Dump last N RA stderr lines + `.proc.poll()` → Task 1 (path) + Task 4 (tail + poll) ✓
- Upgrade `RAHarnessLaunchError` with `pid, port, startup_duration_s, last_stderr_lines` → Task 2 (pid/port/stage/log_path/startup_duration; `last_stderr_lines` is read at diagnostic time via `log_path` so it isn't stored on the exception — equivalent observability, simpler exception) ✓
- Re-raise `pause_toggle` failures with context → Task 5 ✓
- Dashboard timeout message with port + last health-check status → Task 6 ✓
- Strictly inside `tests/integration/` (no `python/spinlab/` source changes) → confirmed across all 6 tasks ✓

**Placeholder scan:** No "TBD", no "implement later", every code step contains the actual code. Test code is concrete with assertions.

**Type consistency check:** `log_path` named identically across `RAHarness`, `RAHarnessLaunchError`, and the diagnostic block. `pid`/`port`/`stage` consistent. `_format_pause_toggle_failure` and `_format_dashboard_startup_failure` are the only two helper names and don't clash with anything in the file.

**Execution note:** Tasks 4–6 all touch `tests/integration/conftest.py` in different regions. If executed as one branch, line numbers cited above will drift after each commit — refer to function names (`_collect_diagnostics`, `fake_dashboard_server`'s retry loops, the `replay_ra_dashboard` pre-yield block) when locating each edit, not raw line numbers.
