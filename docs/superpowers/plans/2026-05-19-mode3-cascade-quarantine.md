# Mode 3 RA Crash Cascade Quarantine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop a single Mode 3 RA crash (Windows ACCESS_VIOLATION, exit code 0xC0000005) from cascading 8–11 cascading NCITimeout/NCIProtocolError failures into the integration test report by health-gating the session-scoped HarnessFactory and surfacing the captured exit code at the failure site.

**Architecture:** Three additive changes wired through the existing test infrastructure:

1. `RAHarness.is_alive()` — synchronous process-poll health probe that captures `proc.returncode` on detected death into a new `last_returncode` field.
2. `HarnessFactory.__call__` — on cache hit, probe `is_alive()`; on detected death, capture the exit code, evict the entry, and raise a typed `RABackendDied` exception so the test that triggered the discovery fails with a clear "RA died with exit code N" report instead of opaque NCITimeouts.
3. `pytest_runtest_makereport` hook extension — when a test body fails with `NCITimeout` or `NCIProtocolError`, walk `item.funcargs` for harness-shaped objects and call `is_alive()` proactively. This captures the exit code while the process is still reapable (before teardown's `_kill` consumes it), so even the first cascading test gets the captured exit code surfaced in its diagnostic block instead of `proc.poll()=None`.

The poisoned cache slot is implicit: an evicted cache entry means the next `__call__` for the same key triggers a fresh `RAHarness.launch()`. No separate "poisoned set" data structure is needed.

**Tech Stack:** Python 3.11, pytest 8.x, subprocess.Popen, NCIClient (UDP). Unit tests use MagicMock(spec=RAHarness) following the pattern at `tests/unit/integration/test_ra_harness_factory.py:131-152`.

**Out of scope:** Fixing the upstream RA stability bug itself (a settle period between `press key=RESET taps=2` and `LOAD_STATE_SLOT`, or a vendored RA patch) is a separate investigation track to start once the cascade is killed.

**Validation:** Per `feedback_stress_test_flakes`, run the full suite (`python -m pytest`) at least 15 sequential times to validate. The Mode 3 baseline rate is 5–10%; the post-fix expectation is: when Mode 3 fires, exactly 1 test fails (with a `RABackendDied` traceback that names the exit code), and subsequent tests run cleanly against a fresh harness — not the current 8–11 cascade pattern.

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `tests/integration/ra_harness.py` | Modify | Add `RABackendDied` exception class, `last_returncode` field on `RAHarness`, and `is_alive()` method. |
| `tests/integration/_harness_factory.py` | Modify | Health-gate cache lookup in `__call__`; eviction on detected death. |
| `tests/integration/_diagnostics.py` | Modify | `collect_diagnostics` reads `harness.last_returncode` as a fallback when `proc.poll()` returns `None` after teardown reaped the process. |
| `tests/integration/conftest.py` | Modify | Extend `pytest_runtest_makereport` to call `harness.is_alive()` on the failing test's fixtures before the report is finalized, so death is captured pre-teardown. Pass the `HarnessFactory` through to the hook via `session.config.stash` so the hook can also evict the poisoned slot. |
| `tests/unit/integration/test_ra_harness.py` | Modify | Add unit tests for `RABackendDied`, `is_alive()`, `last_returncode`. |
| `tests/unit/integration/test_ra_harness_factory.py` | Modify | Add unit tests for cache-lookup health gate + eviction-on-death. |
| `tests/unit/integration/test_diagnostic_hook.py` | Modify | Add unit test that `collect_diagnostics` surfaces `last_returncode` when `proc.poll()` returns None. |

---

## Task 1: Add `RABackendDied` typed exception + `last_returncode` field

**Files:**
- Modify: `tests/integration/ra_harness.py` (add exception class near `RAHarnessLaunchError` at line 77; add field on `RAHarness` dataclass at line 104)
- Test: `tests/unit/integration/test_ra_harness.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/integration/test_ra_harness.py`:

```python
def test_ra_backend_died_carries_structured_fields():
    from tests.integration.ra_harness import RABackendDied

    exc = RABackendDied(
        "RA died with exit code 0xC0000005",
        rom_key="love_yourself",
        pid=12345,
        port=55355,
        exit_code=3221225477,
    )
    assert exc.rom_key == "love_yourself"
    assert exc.pid == 12345
    assert exc.port == 55355
    assert exc.exit_code == 3221225477
    assert "0xC0000005" in str(exc)
    # Must inherit RuntimeError so existing `except RuntimeError:` catches still apply.
    assert isinstance(exc, RuntimeError)


def test_ra_harness_has_last_returncode_default_none():
    """Newly-launched harness has no captured death state."""
    from unittest.mock import MagicMock
    from tests.integration.ra_harness import RAHarness

    harness = RAHarness(
        proc=MagicMock(),
        client=MagicMock(),
    )
    assert harness.last_returncode is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/integration/test_ra_harness.py::test_ra_backend_died_carries_structured_fields tests/unit/integration/test_ra_harness.py::test_ra_harness_has_last_returncode_default_none -v`

Expected: FAIL with `ImportError: cannot import name 'RABackendDied' from 'tests.integration.ra_harness'` (or `AttributeError: 'RAHarness' object has no attribute 'last_returncode'`).

- [ ] **Step 3: Add the exception class and the field**

In `tests/integration/ra_harness.py`, immediately after the `RAHarnessLaunchError` class definition (around line 101), add:

```python
class RABackendDied(RuntimeError):
    """Raised when a cached RAHarness's RA subprocess has died.

    Surfaced by ``HarnessFactory.__call__`` when its health probe finds
    ``proc.poll() is not None`` on a cached harness. Carries the captured
    ``exit_code`` so the failure report distinguishes a crash (e.g. Windows
    ACCESS_VIOLATION 0xC0000005) from an NCI timeout — the difference between
    "RA is hung" and "RA is dead" matters for diagnosis.
    """

    def __init__(
        self,
        message: str,
        *,
        rom_key: str | None = None,
        pid: int | None = None,
        port: int | None = None,
        exit_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.rom_key = rom_key
        self.pid = pid
        self.port = port
        self.exit_code = exit_code
```

Then, in the `RAHarness` dataclass (line 104-117), add a `last_returncode` field after `fresh_boot_slot`:

```python
@dataclass
class RAHarness:
    proc: subprocess.Popen
    client: NCIClient
    # Per-launch RA combined stdout+stderr log. Parent equals _tmp_dir;
    # deleted on teardown — the diagnostic hook must read it before teardown.
    log_path: Path | None = field(default=None, repr=False)
    _log_handle: IO[bytes] | None = field(default=None, repr=False)
    _tmp_dir: Path | None = field(default=None, repr=False)
    fresh_boot_slot: int | None = field(default=None, repr=False)
    # Set by is_alive() the first time it detects proc.poll() != None.
    # Survives teardown's _kill() (which would otherwise reap the process
    # and make proc.returncode unreadable from the diagnostic hook).
    last_returncode: int | None = field(default=None)
    engine: RAPokeEngine = field(init=False)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/unit/integration/test_ra_harness.py::test_ra_backend_died_carries_structured_fields tests/unit/integration/test_ra_harness.py::test_ra_harness_has_last_returncode_default_none -v`

Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add tests/integration/ra_harness.py tests/unit/integration/test_ra_harness.py
git commit -m "tests/integration: add RABackendDied exception + RAHarness.last_returncode field"
```

---

## Task 2: Add `RAHarness.is_alive()` health probe

**Files:**
- Modify: `tests/integration/ra_harness.py` (add method on `RAHarness` after `teardown`, near line 393)
- Test: `tests/unit/integration/test_ra_harness.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/integration/test_ra_harness.py`:

```python
def test_is_alive_returns_true_when_proc_running():
    """proc.poll() is None == still alive."""
    from unittest.mock import MagicMock
    from tests.integration.ra_harness import RAHarness

    proc = MagicMock()
    proc.poll.return_value = None
    harness = RAHarness(proc=proc, client=MagicMock())

    assert harness.is_alive() is True
    assert harness.last_returncode is None  # nothing to capture


def test_is_alive_returns_false_when_proc_dead_and_captures_exit_code():
    """proc.poll() returning a non-None value == dead; capture it."""
    from unittest.mock import MagicMock
    from tests.integration.ra_harness import RAHarness

    proc = MagicMock()
    # 3221225477 == 0xC0000005 == STATUS_ACCESS_VIOLATION on Windows
    proc.poll.return_value = 3221225477
    proc.returncode = 3221225477
    harness = RAHarness(proc=proc, client=MagicMock())

    assert harness.is_alive() is False
    assert harness.last_returncode == 3221225477


def test_is_alive_preserves_last_returncode_across_calls():
    """If proc.poll() later returns None (process reaped), the captured
    last_returncode from a prior is_alive() must NOT be clobbered. This is
    the cascade-survival property — the diagnostic hook can read the
    captured exit code even after teardown reaps the process."""
    from unittest.mock import MagicMock
    from tests.integration.ra_harness import RAHarness

    proc = MagicMock()
    proc.poll.return_value = 3221225477
    proc.returncode = 3221225477
    harness = RAHarness(proc=proc, client=MagicMock())

    assert harness.is_alive() is False
    assert harness.last_returncode == 3221225477

    # Simulate teardown reaping the process: poll() now returns the existing
    # value (still non-None) but in some Popen quirks the proc.returncode can
    # be set to None after wait()+rmtree on Windows. The stored field must
    # survive regardless.
    proc.poll.return_value = None
    proc.returncode = None

    # Subsequent calls: process appears alive again is technically impossible
    # (Popen never resurrects), but if the field were re-cleared on a None
    # poll the diagnostic hook would lose the captured exit code. The contract
    # is: last_returncode is monotonic — once captured, never cleared.
    harness.is_alive()
    assert harness.last_returncode == 3221225477
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/integration/test_ra_harness.py::test_is_alive_returns_true_when_proc_running tests/unit/integration/test_ra_harness.py::test_is_alive_returns_false_when_proc_dead_and_captures_exit_code tests/unit/integration/test_ra_harness.py::test_is_alive_preserves_last_returncode_across_calls -v`

Expected: 3 failures with `AttributeError: 'RAHarness' object has no attribute 'is_alive'`.

- [ ] **Step 3: Add the `is_alive()` method**

In `tests/integration/ra_harness.py`, inside the `RAHarness` class, after `teardown` (around line 411), add:

```python
    def is_alive(self) -> bool:
        """Return True iff the RA subprocess is still running.

        On detected death (``proc.poll()`` returns non-``None``), captures
        ``proc.returncode`` into ``self.last_returncode`` so the value
        survives later teardown reaping. The capture is monotonic — once a
        non-None exit code is stored, subsequent calls do not clear it,
        even if a later ``proc.poll()`` returns ``None`` (Windows Popen
        quirk after wait()+resource cleanup).
        """
        rc = self.proc.poll()
        if rc is None:
            return True
        if self.last_returncode is None:
            # First-time-seen death — capture and log.
            self.last_returncode = rc
            logger.warning(
                "ra_harness: detected dead RA proc pid=%s rc=%s (0x%X)",
                self.proc.pid, rc, rc & 0xFFFFFFFF,
            )
        return False
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/unit/integration/test_ra_harness.py -k is_alive -v`

Expected: 3 passed.

- [ ] **Step 5: Run the broader test_ra_harness module to confirm no regressions**

Run: `python -m pytest tests/unit/integration/test_ra_harness.py -v`

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add tests/integration/ra_harness.py tests/unit/integration/test_ra_harness.py
git commit -m "tests/integration: add RAHarness.is_alive() that captures exit code on death"
```

---

## Task 3: Health-gate `HarnessFactory.__call__`

**Files:**
- Modify: `tests/integration/_harness_factory.py` (lines 45-80, the `__call__` method body)
- Test: `tests/unit/integration/test_ra_harness_factory.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/integration/test_ra_harness_factory.py`:

```python
def test_factory_raises_ra_backend_died_on_cached_dead_harness(tmp_path):
    """When a cached harness's RA proc has died, factory must evict the
    entry, capture the exit code, and raise RABackendDied — NOT silently
    return the dead harness (current behavior) which causes 8-11 follow-on
    NCITimeout cascades."""
    from pathlib import Path
    from unittest.mock import MagicMock, patch

    from tests.integration._harness_factory import harness_factory_impl
    from tests.integration.ra_harness import RABackendDied, RAHarness

    # First call returns the to-be-killed harness; second call returns a
    # successor (verifying that after eviction the cache slot is empty so
    # the next __call__ launches fresh, not raises again).
    dead_harness = MagicMock(spec=RAHarness)
    dead_harness.is_alive.return_value = False
    dead_harness.last_returncode = 3221225477  # 0xC0000005
    dead_harness.proc = MagicMock()
    dead_harness.proc.pid = 12345
    dead_harness.client = MagicMock()
    dead_harness.client.port = 55001

    fresh_harness = MagicMock(spec=RAHarness)
    fresh_harness.is_alive.return_value = True

    launched = [dead_harness, fresh_harness]
    with patch(
        "tests.integration._harness_factory.RAHarness.launch",
        side_effect=lambda **kw: launched.pop(0),
    ), patch(
        "tests.integration._harness_factory.resolve_ra_paths",
        return_value=(Path("exe"), Path("core"), Path("rom")),
    ), patch(
        "tests.integration._harness_factory._free_udp_port",
        side_effect=[55001, 55002],
    ):
        factory_impl = harness_factory_impl()
        h1 = factory_impl("vanilla_smw")
        assert h1 is dead_harness  # initial launch returns the to-be-dead one

        # Simulate the RA process crashing between tests
        dead_harness.is_alive.return_value = False

        # Next factory call: health probe catches it, raises typed exception
        with pytest.raises(RABackendDied) as excinfo:
            factory_impl("vanilla_smw")

    assert excinfo.value.rom_key == "vanilla_smw"
    assert excinfo.value.pid == 12345
    assert excinfo.value.port == 55001
    assert excinfo.value.exit_code == 3221225477
    assert "0xC0000005" in str(excinfo.value)


def test_factory_relaunches_after_dead_harness_evicted(tmp_path):
    """After RABackendDied is raised, the cache entry must be gone so the
    next __call__ launches a fresh RA process."""
    from pathlib import Path
    from unittest.mock import MagicMock, patch

    from tests.integration._harness_factory import harness_factory_impl
    from tests.integration.ra_harness import RABackendDied, RAHarness

    dead_harness = MagicMock(spec=RAHarness)
    dead_harness.is_alive.return_value = True  # alive on first lookup
    dead_harness.last_returncode = None
    dead_harness.proc = MagicMock()
    dead_harness.proc.pid = 12345
    dead_harness.client = MagicMock()
    dead_harness.client.port = 55001

    fresh_harness = MagicMock(spec=RAHarness)
    fresh_harness.is_alive.return_value = True

    launched = [dead_harness, fresh_harness]
    with patch(
        "tests.integration._harness_factory.RAHarness.launch",
        side_effect=lambda **kw: launched.pop(0),
    ), patch(
        "tests.integration._harness_factory.resolve_ra_paths",
        return_value=(Path("exe"), Path("core"), Path("rom")),
    ), patch(
        "tests.integration._harness_factory._free_udp_port",
        side_effect=[55001, 55002],
    ):
        factory_impl = harness_factory_impl()
        h1 = factory_impl("vanilla_smw")
        assert h1 is dead_harness

        # Simulate the RA process crashing
        dead_harness.is_alive.return_value = False
        dead_harness.last_returncode = 3221225477

        with pytest.raises(RABackendDied):
            factory_impl("vanilla_smw")

        # The crash has been surfaced once. Subsequent call should launch
        # a FRESH harness — the cache entry was evicted.
        h2 = factory_impl("vanilla_smw")
        assert h2 is fresh_harness
        assert h2 is not dead_harness


def test_factory_returns_cached_harness_when_alive(tmp_path):
    """Sanity: the health gate must NOT evict a live harness on cache hit
    (would cause a fresh launch per test, defeating the cache)."""
    from pathlib import Path
    from unittest.mock import MagicMock, patch

    from tests.integration._harness_factory import harness_factory_impl
    from tests.integration.ra_harness import RAHarness

    live_harness = MagicMock(spec=RAHarness)
    live_harness.is_alive.return_value = True

    with patch(
        "tests.integration._harness_factory.RAHarness.launch",
        return_value=live_harness,
    ), patch(
        "tests.integration._harness_factory.resolve_ra_paths",
        return_value=(Path("exe"), Path("core"), Path("rom")),
    ), patch(
        "tests.integration._harness_factory._free_udp_port",
        return_value=55001,
    ):
        factory_impl = harness_factory_impl()
        h1 = factory_impl("vanilla_smw")
        h2 = factory_impl("vanilla_smw")
        h3 = factory_impl("vanilla_smw")

    assert h1 is live_harness
    assert h1 is h2 is h3
    # is_alive() called once per cache hit after the first (the first call
    # is a cache miss, no probe needed). 2 hits = 2 probes.
    assert live_harness.is_alive.call_count == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/integration/test_ra_harness_factory.py::test_factory_raises_ra_backend_died_on_cached_dead_harness tests/unit/integration/test_ra_harness_factory.py::test_factory_relaunches_after_dead_harness_evicted tests/unit/integration/test_ra_harness_factory.py::test_factory_returns_cached_harness_when_alive -v`

Expected: 3 failures. The first two with `Failed: DID NOT RAISE <class 'RABackendDied'>`; the third may pass already (current code returns cached without probing) — verify the assertion `live_harness.is_alive.call_count == 2` fails specifically.

- [ ] **Step 3: Add health-gate to `__call__`**

In `tests/integration/_harness_factory.py`, modify the `__call__` method (lines 45-80). Replace its body with:

```python
    def __call__(self, rom_key: str, use_fresh_state: bool = True) -> RAHarness:
        """Return (or create + cache) a harness for `rom_key`.

        On cache hit, probes the cached harness via ``is_alive()``; a dead
        RA proc (Windows ACCESS_VIOLATION or otherwise) triggers eviction
        and raises ``RABackendDied`` with the captured exit code. The next
        call for the same key launches a fresh harness.

        `use_fresh_state=True` (the default) wires a per-launch isolated
        savestate_directory with the fresh-boot state pre-staged at
        FRESH_BOOT_STATE_SLOT, and causes RAPokeEngine to load it before
        each scenario. Required by the poke-transition tests.

        `use_fresh_state=False` is for fixtures whose RA process must talk
        to the user's actual savestate_directory — currently just the
        replay fixture.
        """
        cache_key = (rom_key, use_fresh_state)
        cached = self._cache.get(cache_key)
        if cached is not None:
            if cached.is_alive():
                return cached
            # Dead harness: capture context BEFORE eviction so the typed
            # exception carries pid/port for the diagnostic hook.
            exit_code = cached.last_returncode
            pid = cached.proc.pid
            port = cached.client.port
            del self._cache[cache_key]
            exit_code_hex = (
                f"0x{exit_code & 0xFFFFFFFF:X}" if exit_code is not None else "<unknown>"
            )
            raise RABackendDied(
                f"RA died with exit code {exit_code_hex} "
                f"(rom_key={rom_key!r}, pid={pid}, port={port})",
                rom_key=rom_key,
                pid=pid,
                port=port,
                exit_code=exit_code,
            )
        retroarch_exe, ra_core_path, rom_path = resolve_ra_paths(rom_key)
        fresh_state_path = (
            state_path_for(ROM_REGISTRY[rom_key]) if use_fresh_state else None
        )
        try:
            harness = RAHarness.launch(
                rom_path=rom_path,
                core_path=ra_core_path,
                retroarch_exe=retroarch_exe,
                nci_port=_free_udp_port(),
                fresh_state_path=fresh_state_path,
            )
        except RAHarnessLaunchError as exc:
            # CLAUDE.md: launch failure is a FAILURE, not a skip. Annotate args
            # with rom_key so the test report still names the harness that failed.
            exc.args = (
                f"ra_harness launch failed for rom_key={rom_key!r}: {exc.args[0]}",
            )
            raise
        self._cache[cache_key] = harness
        return harness
```

Also update the import at the top of the file (line 16-19):

```python
from tests.integration.ra_harness import (
    RABackendDied,
    RAHarness,
    RAHarnessLaunchError,
)
```

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `python -m pytest tests/unit/integration/test_ra_harness_factory.py::test_factory_raises_ra_backend_died_on_cached_dead_harness tests/unit/integration/test_ra_harness_factory.py::test_factory_relaunches_after_dead_harness_evicted tests/unit/integration/test_ra_harness_factory.py::test_factory_returns_cached_harness_when_alive -v`

Expected: 3 passed.

- [ ] **Step 5: Run the full factory test module to confirm no regressions**

Run: `python -m pytest tests/unit/integration/test_ra_harness_factory.py -v`

Expected: all tests pass (including the existing `test_factory_caches_per_key`, `test_factory_raises_runtime_error_on_launch_failure`, `test_factory_propagates_ra_harness_launch_error_with_typed_fields`).

Note: `test_factory_caches_per_key` uses `MagicMock(spec=RAHarness)` — these mocks will now have `is_alive()` called on them. MagicMock auto-returns truthy MagicMock for any method, so the test will keep passing (the mock returns alive-ish by default). If that test starts failing, add `h_vanilla.is_alive.return_value = True; h_love.is_alive.return_value = True` after constructing the mocks.

- [ ] **Step 6: Commit**

```bash
git add tests/integration/_harness_factory.py tests/unit/integration/test_ra_harness_factory.py
git commit -m "tests/integration: health-gate HarnessFactory cache lookup with RABackendDied"
```

---

## Task 4: Surface `last_returncode` in `collect_diagnostics`

**Files:**
- Modify: `tests/integration/_diagnostics.py` (the harness-shaped branch, lines 176-194)
- Test: `tests/unit/integration/test_diagnostic_hook.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/integration/test_diagnostic_hook.py`:

```python
def test_collect_diagnostics_surfaces_captured_returncode_when_proc_already_reaped():
    """When proc.poll() returns None (process already reaped by teardown)
    but harness.last_returncode is set, the diagnostic block must show the
    captured exit code rather than 'proc.poll()=None'. This is the
    cross-test-cascade case: test 1 crashes RA, teardown reaps the process,
    tests 2-11 see proc.poll()=None and the crash signature is lost UNLESS
    we use the captured field."""
    from unittest.mock import MagicMock

    from tests.integration._diagnostics import collect_diagnostics

    harness = MagicMock()
    harness.proc.poll.return_value = None  # already reaped
    harness.proc.pid = 12345
    harness.client.port = 55001
    # 3221225477 == 0xC0000005 captured by is_alive() before teardown
    harness.last_returncode = 3221225477
    harness.log_path = None

    item = MagicMock()
    item.funcargs = {"ra_harness_love_yourself": harness}

    out = collect_diagnostics(item)

    # Must show the captured exit code, not "None":
    assert "0xC0000005" in out or "3221225477" in out
    assert "last_returncode" in out  # field name for searchability


def test_collect_diagnostics_falls_back_to_proc_poll_when_no_capture():
    """When last_returncode is None (no capture happened) the diagnostic
    output keeps the existing proc.poll() shape so we don't regress
    pre-cascade reports."""
    from unittest.mock import MagicMock

    from tests.integration._diagnostics import collect_diagnostics

    harness = MagicMock()
    harness.proc.poll.return_value = None
    harness.proc.pid = 12345
    harness.client.port = 55001
    harness.last_returncode = None
    harness.log_path = None

    item = MagicMock()
    item.funcargs = {"ra_harness_love_yourself": harness}

    out = collect_diagnostics(item)
    assert "proc.poll()=None" in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/integration/test_diagnostic_hook.py::test_collect_diagnostics_surfaces_captured_returncode_when_proc_already_reaped tests/unit/integration/test_diagnostic_hook.py::test_collect_diagnostics_falls_back_to_proc_poll_when_no_capture -v`

Expected: First test fails (no "0xC0000005" in output). Second test probably passes already.

- [ ] **Step 3: Update the harness diagnostic branch**

In `tests/integration/_diagnostics.py`, modify the harness-shaped branch (lines 176-194). Replace the block starting at `if hasattr(fixture_val, "proc") and hasattr(fixture_val, "client"):` with:

```python
        # ---- Harness-shaped: duck-types on .proc + .client ----
        if hasattr(fixture_val, "proc") and hasattr(fixture_val, "client"):
            # Duck-typed access — `fixture_val` is typed as `object | tuple`
            # from the dashboard branch above; `hasattr` doesn't narrow.
            harness = cast(Any, fixture_val)
            try:
                proc_status = harness.proc.poll()
            except Exception as exc:
                proc_status = f"<poll failed: {exc}>"
            try:
                port = harness.client.port
            except Exception:
                port = "<unknown>"
            try:
                pid = harness.proc.pid
            except Exception:
                pid = "<unknown>"
            # If teardown already reaped the process, proc.poll() returns
            # None and the crash signature is lost — UNLESS is_alive() ran
            # before teardown and captured the exit code into last_returncode.
            last_rc = getattr(fixture_val, "last_returncode", None)
            if proc_status is None and last_rc is not None:
                proc_status_str = (
                    f"reaped, last_returncode={last_rc} (0x{last_rc & 0xFFFFFFFF:X})"
                )
            else:
                proc_status_str = f"{proc_status}"
            parts.append(
                f"  harness: {fixture_name} pid={pid} port={port} "
                f"proc.poll()={proc_status_str}"
            )
            log_path = getattr(fixture_val, "log_path", None)
            if log_path is not None:
                try:
                    if log_path.exists():
                        text = log_path.read_text(errors="replace")
                        tail = text.splitlines()[-HARNESS_LOG_TAIL_LINES:]
                        if tail:
                            parts.append(f"  retroarch.log tail ({len(tail)} lines):")
                            for line in tail:
                                parts.append(f"    {line}")
                except Exception as exc:
                    parts.append(f"  retroarch.log: <unavailable: {exc}>")
```

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `python -m pytest tests/unit/integration/test_diagnostic_hook.py::test_collect_diagnostics_surfaces_captured_returncode_when_proc_already_reaped tests/unit/integration/test_diagnostic_hook.py::test_collect_diagnostics_falls_back_to_proc_poll_when_no_capture -v`

Expected: 2 passed.

- [ ] **Step 5: Run the full diagnostic-hook test module to confirm no regressions**

Run: `python -m pytest tests/unit/integration/test_diagnostic_hook.py -v`

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add tests/integration/_diagnostics.py tests/unit/integration/test_diagnostic_hook.py
git commit -m "tests/integration: surface RAHarness.last_returncode in cascade diagnostics"
```

---

## Task 5: Pre-teardown cascade probe in `pytest_runtest_makereport`

**Files:**
- Modify: `tests/integration/conftest.py` (the `pytest_runtest_makereport` hook at lines 311-350)
- Test: `tests/unit/integration/test_diagnostic_hook.py` (or new section in same file)

The goal: when a test body raises `NCITimeout` or `NCIProtocolError` (the cascade signature), proactively call `harness.is_alive()` on every harness-shaped funcarg BEFORE the diagnostic block is rendered. This captures the exit code while `proc.returncode` is still readable (before the session teardown's `_kill()` consumes it). Even the *first* test in a Mode 3 cascade then gets the captured exit code in its diagnostic block, instead of `proc.poll()=None`.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/integration/test_diagnostic_hook.py`:

```python
def test_makereport_hook_calls_is_alive_on_nci_failure():
    """When a test fails with NCITimeout, the diagnostic hook must probe
    is_alive() on every harness-shaped funcarg before rendering — so the
    captured exit code from a Mode 3 crash lands in the report BEFORE
    teardown reaps the process."""
    from unittest.mock import MagicMock
    from spinlab.retroarch.exceptions import NCITimeout
    from tests.integration.conftest import _probe_harnesses_on_nci_failure

    harness = MagicMock()
    harness.is_alive.return_value = False
    harness.last_returncode = None

    item = MagicMock()
    item.funcargs = {"ra_harness_love_yourself": harness}

    excinfo = MagicMock()
    excinfo.value = NCITimeout("no reply within 0.5s for 'READ_CORE_RAM ...'")

    _probe_harnesses_on_nci_failure(item, excinfo)

    # is_alive() was called; harness can now report the captured exit code
    # via collect_diagnostics in the next step of the hook chain.
    harness.is_alive.assert_called_once()


def test_makereport_hook_skips_non_nci_failures():
    """Non-NCI failures (e.g. AssertionError) should NOT trigger the probe —
    saves time and avoids probing a healthy harness on every test failure."""
    from unittest.mock import MagicMock
    from tests.integration.conftest import _probe_harnesses_on_nci_failure

    harness = MagicMock()
    harness.is_alive.return_value = True

    item = MagicMock()
    item.funcargs = {"ra_harness_love_yourself": harness}

    excinfo = MagicMock()
    excinfo.value = AssertionError("test failure unrelated to NCI")

    _probe_harnesses_on_nci_failure(item, excinfo)

    harness.is_alive.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/integration/test_diagnostic_hook.py::test_makereport_hook_calls_is_alive_on_nci_failure tests/unit/integration/test_diagnostic_hook.py::test_makereport_hook_skips_non_nci_failures -v`

Expected: 2 failures with `ImportError: cannot import name '_probe_harnesses_on_nci_failure'`.

- [ ] **Step 3: Add the probe helper + wire it into the hook**

In `tests/integration/conftest.py`, add the helper function near the bottom of the imports block (around line 50, before `pytestmark`):

```python
def _probe_harnesses_on_nci_failure(item, excinfo) -> None:
    """Proactively call is_alive() on every harness-shaped funcarg.

    Runs when a test body raises NCITimeout / NCIProtocolError — the Mode 3
    crash cascade signature. Captures proc.returncode into harness.last_returncode
    BEFORE the session-scoped fixture teardown reaps the process, so even the
    first cascading test's diagnostic block can show "0xC0000005" instead of
    "proc.poll()=None".

    Idempotent and safe on healthy harnesses: is_alive() returns True without
    side effect.
    """
    from spinlab.retroarch.exceptions import NCIError

    exc = excinfo.value if hasattr(excinfo, "value") else excinfo
    if not isinstance(exc, NCIError):
        return
    funcargs = getattr(item, "funcargs", {})
    for fixture_val in funcargs.values():
        if hasattr(fixture_val, "is_alive"):
            try:
                fixture_val.is_alive()
            except Exception:
                # Diagnostic code must never crash the report rendering.
                pass
```

Then, in the `pytest_runtest_makereport` hook (lines 311-350), call the new helper inside the `report.when == "call"` branch BEFORE `collect_diagnostics`:

```python
    if report.when == "call":
        # Proactively capture RA exit code before teardown reaps the process.
        # Only fires on NCITimeout / NCIProtocolError — the cascade signature.
        if call.excinfo is not None:
            _probe_harnesses_on_nci_failure(item, call.excinfo)
        diag = collect_diagnostics(item)
        if diag:
            # `longreprtext` is a read-only property in current pytest, so the
            # diagnostic block has to ride along on `sections` instead.  pytest
            # renders sections in the terminal report after the traceback.
            report.sections.append(("SpinLab Diagnostics", diag))
        return
```

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `python -m pytest tests/unit/integration/test_diagnostic_hook.py::test_makereport_hook_calls_is_alive_on_nci_failure tests/unit/integration/test_diagnostic_hook.py::test_makereport_hook_skips_non_nci_failures -v`

Expected: 2 passed.

- [ ] **Step 5: Run all unit tests under tests/unit/integration/ to confirm no regressions**

Run: `python -m pytest tests/unit/integration/ -v`

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add tests/integration/conftest.py tests/unit/integration/test_diagnostic_hook.py
git commit -m "tests/integration: probe harness is_alive() pre-teardown on NCI cascade"
```

---

## Task 6: Document Mode 3 mitigation in CLAUDE.md

**Files:**
- Modify: `CLAUDE.md` (the "Gotchas" section, near line 75 — where similar RA-quirk notes live)

- [ ] **Step 1: Add a CLAUDE.md note**

In `CLAUDE.md`, find the "### Gotchas" subsection (currently includes the hardcore_mode, run_ahead, and log_to_file notes). Append a new bullet:

```markdown
- RA can crash mid-session with Windows ACCESS_VIOLATION (0xC0000005) after the cold-fill RESET sequence. The integration suite quarantines the cascade: `HarnessFactory` health-gates cached harnesses via `RAHarness.is_alive()`; on detected death the cache slot is evicted and `RABackendDied` is raised with the captured exit code (so the failure report names the crash instead of opaque NCITimeouts). The `pytest_runtest_makereport` hook also probes is_alive() proactively on NCITimeout / NCIProtocolError test failures, so the captured exit code lands in the first cascading test's diagnostic block rather than getting lost when teardown reaps the process. The upstream RA stability bug (settle period between `press key=RESET taps=2` and `LOAD_STATE_SLOT`?) is a separate investigation track.
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: CLAUDE.md note on Mode 3 RA crash cascade quarantine"
```

---

## Task 7: Full-suite baseline + 15-iteration stress validation

**Files:** None modified — this is the validation gate.

Per `feedback_stress_test_flakes`: evidence for a flake fix is N/15+ sequential runs, not single observations. The Mode 3 baseline rate is 5–10%; after this plan lands, the expectation is:

- The full suite passes more consistently overall (cascade no longer multiplies one crash into 8–11 failures).
- When Mode 3 still fires (root cause is upstream RA), exactly 1 test fails with a `RABackendDied` traceback that names the exit code, AND the diagnostic block shows `last_returncode=3221225477 (0xC0000005)` — not `proc.poll()=None` cascades.

- [ ] **Step 1: Run a single full-suite baseline**

Run: `python -m pytest 2>&1 | tee C:\tmp\mode3-baseline-single.log`

Read the tail and confirm:
- Either all tests pass, OR
- Any failure is investigated before continuing (per CLAUDE.md baseline rule).

- [ ] **Step 2: Run 15 sequential full-suite iterations**

Run (PowerShell):

```powershell
$logPath = "C:\tmp\mode3-stress-15.log"
Remove-Item $logPath -ErrorAction SilentlyContinue
for ($i = 1; $i -le 15; $i++) {
    "=== Run $i ===" | Out-File -Append $logPath
    python -m pytest --tb=line -q 2>&1 | Out-File -Append $logPath
}
```

Expected duration: ~15 minutes (52s × 15 + overhead). Capture in background.

- [ ] **Step 3: Count failures and inspect cascade shape**

Run:

```powershell
Select-String -Path C:\tmp\mode3-stress-15.log -Pattern "^=== Run|failed in|RABackendDied|NCITimeout|0xC0000005|3221225477"
```

Pass criteria:
- Total runs with ≥1 failure ≤ 2/15 (baseline was 1-2/15; we're not fixing the upstream crash, just the cascade).
- When a failure occurs, the failing test count is ≤ 2 (not 8-11).
- At least one failing diagnostic shows `RABackendDied` and/or `last_returncode=...` text.

Fail criteria (do NOT declare success):
- ≥8 tests failing in any single run after the first cascading failure (cascade-not-fixed signature).
- Any failure that says `proc.poll()=None` for all cascading tests but no captured exit code anywhere (probe didn't fire).

- [ ] **Step 4: Report results to the user**

Reply with the exact ratio ("X/15 runs with failures, Y total failed tests across all runs, Z runs showed RABackendDied"). Do NOT claim "fixed" unless the cascade-shape criterion above is met.

---

## Self-Review

**Spec coverage check:**
- Goal (1) "detect dead RA at harness-lookup time via proc.poll() + cheap NCI probe; on death, capture proc.returncode, log it" → Task 2 (is_alive) + Task 3 (factory health gate).
- Goal (2) "Add a pytest_runtest_makereport hook that ... marks the cached harness slot as poisoned" → Task 5 (proactive probe) + Task 3 (eviction-on-death gives the equivalent "next test relaunches" behavior without a separate poisoned set).
- Goal (3) "Capture proc.returncode at the moment of detected death and stash it on the harness for diagnostic surface" → Task 1 (last_returncode field) + Task 2 (capture site) + Task 4 (diagnostic surfacing).
- Out-of-scope ("settle period between RESET and LOAD_STATE_SLOT, vendored RA patch") explicitly deferred — Tasks 6/7 mention but do not implement.

**Placeholder scan:** No TBDs, no "implement later," no "similar to Task N." Every test body, exception class, and implementation block is fully written out.

**Type consistency:**
- `RABackendDied(rom_key, pid, port, exit_code)` — same field names used in Task 1 (definition), Task 3 (factory raises), Task 4 (diagnostic reads).
- `RAHarness.last_returncode: int | None` — same name in Task 1 (field), Task 2 (capture site), Task 4 (diagnostic reads), Task 5 (probe captures).
- `RAHarness.is_alive() -> bool` — same signature in Task 2 (definition), Task 3 (factory calls), Task 5 (hook calls).

**One known nuance:** Task 3 changes the behavior of cached mocks in pre-existing tests like `test_factory_caches_per_key` (line 125 of test_ra_harness_factory.py) — they pass `MagicMock(spec=RAHarness)` which auto-returns truthy for `is_alive()` but may need explicit `.is_alive.return_value = True` if `MagicMock(spec=...)` later tightens. Documented in Task 3 Step 5.
