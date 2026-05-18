# CF-1: Test infra helpers — dashboard_harness + wait_for + conftest decomposition

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract three reusable utilities (`wait_for` typed polling helper, `DashboardHarness` context manager, decomposed support modules) from the 871-line `tests/integration/conftest.py` so the duplicated dashboard-startup code becomes one helper, timeouts always name what they were waiting on, and the unit-tests-for-integration-machinery directory stops reaching into private conftest symbols.

**Architecture:** Move test-infra concerns out of `conftest.py` and into siblings that unit tests can import without leaks: `_rom_paths.py` (ROM registry + resolvers), `_diagnostics.py` (ring buffer + collectors + formatters), `_harness_factory.py` (RAHarness session cache), `_wait_for.py` (typed polling helper with `WaitOutcome` dataclass naming what timed out), `_dashboard_harness.py` (context manager that owns tmpdir + Database + uvicorn thread lifecycle, swapped fake backend optional). `conftest.py` keeps only the pytest fixture surface and the two pytest hooks; the fixtures delegate to the new helpers.

**Tech Stack:** Python 3.11+, pytest, pytest_asyncio, uvicorn, requests. No new dependencies. Type hints throughout (project uses `npx pyright python/` for static checking).

**Branch:** `improve/test-infra-helpers-and-typed-fixtures` (already created; CF-2 + CF-3-obs already shipped on it).

**Scan reference:** `docs/superpowers/scans/2026-05-17-improve-2220.md` — Top wins → CF-1.

---

## File structure

**New files:**

- `tests/integration/_wait_for.py` — `WaitOutcome` dataclass + `wait_for()` generic polling helper. No pytest dependency; pure utility.
- `tests/integration/_rom_paths.py` — `ROM_REGISTRY`, `resolve_rom_path()`, `resolve_ra_paths()`, `load_config()`, `state_path_for()`, `INTEGRATION_STATES_DIR`. Moved verbatim from conftest, underscore prefixes dropped from public names.
- `tests/integration/_diagnostics.py` — `RingHandler`, `ring` module-level instance, `install_log_handler()`, `collect_diagnostics()`, `collect_launch_failure_diagnostics()`, `format_pause_toggle_failure()`, `format_dashboard_startup_failure()`. Conftest must call `install_log_handler()` once at import time.
- `tests/integration/_harness_factory.py` — `HarnessFactory` class + `harness_factory_impl()` constructor. Depends on `_rom_paths` and on `RAHarness` / `RAHarnessLaunchError`.
- `tests/integration/_dashboard_harness.py` — `DashboardHarness` context manager: `with DashboardHarness(...) as (base_url, db, session):` owns tmpdir, Database, AppConfig, uvicorn thread. Optional `fake_emu_backend: bool` swap. Used by both `fake_dashboard_server` and `replay_ra_dashboard`.

**Modified files:**

- `tests/integration/conftest.py` — drops the moved code (~300 lines removed). Keeps `pytest_runtest_makereport` and `pytest_runtest_setup` hooks (they must live in conftest for pytest auto-discovery). Keeps fixture definitions but they now delegate to helpers. Calls `install_log_handler()` once at module load. Expected final length: ~400 lines (down from 871).
- `tests/integration/test_replay_fixture.py` — `_wait_for_replay_mode` (lines 38-52) rewritten to use new `wait_for()` so the timeout message names whether `mode` or `replay.total` was the unmet condition.
- `tests/unit/integration/test_diagnostic_hook.py` — import from `tests.integration._diagnostics` instead of `tests.integration.conftest`.
- `tests/unit/integration/test_ra_harness_factory.py` — import from `tests.integration._rom_paths` and `tests.integration._harness_factory` instead of `tests.integration.conftest`.

**No production code (`python/spinlab/`) is modified by this plan.**

---

## Task 1: `wait_for` typed polling helper (TDD)

**Files:**
- Create: `tests/integration/_wait_for.py`
- Test: `tests/unit/integration/test_wait_for.py` (new)

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/integration/test_wait_for.py
"""Tests for the typed wait_for polling helper used by integration fixtures."""
from __future__ import annotations

import time

import pytest

from tests.integration._wait_for import WaitOutcome, wait_for


def test_wait_for_returns_succeeded_outcome_when_predicate_true_immediately():
    state = {"ready": True}
    outcome = wait_for(
        name="ready_flag",
        fetch=lambda: state,
        predicate=lambda s: (s["ready"], ""),
        timeout_s=1.0,
        interval_s=0.05,
    )
    assert outcome.succeeded is True
    assert outcome.name == "ready_flag"
    assert outcome.attempts == 1
    assert outcome.last_reason == ""


def test_wait_for_returns_timeout_outcome_when_predicate_never_true():
    outcome = wait_for(
        name="never_ready",
        fetch=lambda: {"ready": False},
        predicate=lambda s: (s["ready"], f"ready={s['ready']}"),
        timeout_s=0.2,
        interval_s=0.05,
    )
    assert outcome.succeeded is False
    assert outcome.name == "never_ready"
    assert outcome.attempts >= 1
    assert outcome.last_reason == "ready=False"
    assert outcome.elapsed_s >= 0.2


def test_wait_for_eventually_succeeds():
    counter = {"n": 0}

    def fetch():
        counter["n"] += 1
        return counter["n"]

    outcome = wait_for(
        name="counter_reaches_3",
        fetch=fetch,
        predicate=lambda n: (n >= 3, f"n={n}"),
        timeout_s=1.0,
        interval_s=0.01,
    )
    assert outcome.succeeded is True
    assert counter["n"] >= 3
    assert outcome.last_reason == ""


def test_wait_for_records_fetch_exception_as_last_reason():
    def fetch():
        raise RuntimeError("fetch boom")

    outcome = wait_for(
        name="boomy",
        fetch=fetch,
        predicate=lambda _v: (True, ""),  # never reached
        timeout_s=0.15,
        interval_s=0.05,
    )
    assert outcome.succeeded is False
    assert "fetch boom" in outcome.last_reason
    assert "RuntimeError" in outcome.last_reason


def test_wait_outcome_format_message_includes_name_elapsed_attempts_reason():
    outcome = WaitOutcome(
        succeeded=False,
        name="orchestrator_ready",
        elapsed_s=2.5,
        attempts=10,
        last_reason="emu_connected=False game_id=None",
    )
    msg = outcome.format_message()
    assert "orchestrator_ready" in msg
    assert "2.5" in msg
    assert "10" in msg
    assert "emu_connected=False game_id=None" in msg


def test_wait_for_succeeded_outcome_format_message_is_concise():
    outcome = WaitOutcome(
        succeeded=True, name="ok", elapsed_s=0.1, attempts=1, last_reason="",
    )
    msg = outcome.format_message()
    assert "ok" in msg
    assert "succeeded" in msg.lower()
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
python -m pytest tests/unit/integration/test_wait_for.py -v
```

Expected: FAIL with `ModuleNotFoundError: tests.integration._wait_for`.

- [ ] **Step 3: Implement `_wait_for.py`**

```python
# tests/integration/_wait_for.py
"""Typed polling helper used by integration fixtures and test bodies.

Wraps the "poll a fetch() until predicate(value) is True, or time out" loop
in a single function so every call site reports a structured outcome that
names the operation, the elapsed time, the attempt count, and why the
predicate was last unsatisfied. The old ad-hoc helpers (e.g. the original
`_wait_for_dashboard_state`) returned only `last_error` and lost the name
of what was being waited on.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class WaitOutcome:
    """Result of a `wait_for` call. `succeeded=True` means the predicate
    returned `(True, ...)` before the deadline.
    """

    succeeded: bool
    name: str
    elapsed_s: float
    attempts: int
    last_reason: str  # empty string on success

    def format_message(self) -> str:
        if self.succeeded:
            return (
                f"wait_for({self.name}) succeeded after "
                f"{self.attempts} attempt(s) in {self.elapsed_s:.2f}s"
            )
        return (
            f"wait_for({self.name}) timed out after "
            f"{self.attempts} attempt(s) in {self.elapsed_s:.2f}s; "
            f"last reason: {self.last_reason}"
        )


def wait_for(
    *,
    name: str,
    fetch: Callable[[], T],
    predicate: Callable[[T], tuple[bool, str]],
    timeout_s: float = 10.0,
    interval_s: float = 0.25,
) -> WaitOutcome:
    """Poll `fetch()` until `predicate(value)` returns `(True, _)` or `timeout_s` elapses.

    `predicate` returns `(ok, reason)`. When `ok=False`, `reason` describes
    why the predicate was unsatisfied so the timeout message can be specific
    instead of "Last state: <dump>". `reason` is ignored when `ok=True`.

    If `fetch()` raises, the exception's `type(__name__): str(exc)` becomes
    the next `last_reason` and polling continues until the deadline.
    """
    start = time.monotonic()
    deadline = start + timeout_s
    attempts = 0
    last_reason = ""
    while True:
        attempts += 1
        try:
            value = fetch()
            ok, reason = predicate(value)
            if ok:
                return WaitOutcome(
                    succeeded=True, name=name,
                    elapsed_s=time.monotonic() - start,
                    attempts=attempts, last_reason="",
                )
            last_reason = reason
        except Exception as exc:
            last_reason = f"{type(exc).__name__}: {exc}"
        if time.monotonic() >= deadline:
            return WaitOutcome(
                succeeded=False, name=name,
                elapsed_s=time.monotonic() - start,
                attempts=attempts, last_reason=last_reason,
            )
        time.sleep(interval_s)
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
python -m pytest tests/unit/integration/test_wait_for.py -v
```

Expected: 6 passed.

- [ ] **Step 5: Type-check the new module**

```bash
npx pyright tests/integration/_wait_for.py tests/unit/integration/test_wait_for.py
```

Expected: `0 errors, 0 warnings, 0 informations`.

- [ ] **Step 6: Commit**

```bash
git add tests/integration/_wait_for.py tests/unit/integration/test_wait_for.py
git commit -m "tests/integration: add typed wait_for polling helper

WaitOutcome dataclass + wait_for() generic loop; predicate returns
(ok, reason) so timeout messages name what failed. First building
block of CF-1; not yet wired into the fixtures."
```

---

## Task 2: Move ROM registry + path resolvers to `_rom_paths.py`

**Files:**
- Create: `tests/integration/_rom_paths.py`
- Modify: `tests/integration/conftest.py:36-87, 90-161` (remove moved code, add import + back-compat aliases for the in-module callers)
- Modify: `tests/unit/integration/test_ra_harness_factory.py:14-15, 31, 38, 50, 58, 67, 76, 89, 102, 117, 130, 143` (each `from tests.integration.conftest import _resolve_rom_path` and similar — switch to new module, also drop underscore prefix)

- [ ] **Step 1: Write the new module**

```python
# tests/integration/_rom_paths.py
"""ROM registry + path resolvers for integration tests.

Pulled out of `tests/integration/conftest.py` so unit tests under
`tests/unit/integration/` can import these as a real module rather than
reaching into private conftest symbols.
"""
from __future__ import annotations

from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]

LOVE_YOURSELF_ROM_NAME = "Love Yourself.smc"
LOVE_YOURSELF_GAME_ID = "bd94dbb29012c7f5"

TOOTHPASTE_ROM_NAME = "Toothpaste.smc"
CLEAN_SMW_ROM_NAME = "_clean.smc"

# rom_key -> filename in config.yaml's rom.dir.
# Each entry produces one cached session-scoped RAHarness in `ra_harness_factory`.
# Tests must declare which ROM they need via the matching named fixture
# (e.g. `ra_harness_vanilla_smw`); there is no implicit `default` fallback.
#
# Adding a new ROM requires THREE things:
#   1. A `<rom_key>: "<rom_filename>"` line below.
#   2. A fresh-boot savestate at tests/integration/states/<rom_basename>.state,
#      generated via `python scripts/make_fresh_boot_state.py --rom-key <key>`.
#      Without it, RAPokeEngine can't reset between scenarios and tests share
#      ROM CPU/SPC state across runs (project_transition_state_leak).
#   3. A `ra_harness_<rom_key>` fixture in conftest.py that calls
#      `ra_harness_factory("<key>")`. Tests reference fixtures by name.
ROM_REGISTRY: dict[str, str] = {
    "vanilla_smw": CLEAN_SMW_ROM_NAME,
    "love_yourself": LOVE_YOURSELF_ROM_NAME,
    "toothpaste": TOOTHPASTE_ROM_NAME,
}

# Per-ROM "fresh boot" savestate. Generated by `scripts/make_fresh_boot_state.py`
# and committed under tests/integration/states/. Loaded by RAPokeEngine before
# every scenario so the session-scoped harness doesn't leak SPC700/CPU state
# between transition scenarios.
INTEGRATION_STATES_DIR = Path(__file__).resolve().parent / "states"


def load_config() -> dict:
    """Load config.yaml from project root."""
    config_path = PROJECT_ROOT / "config.yaml"
    if config_path.exists():
        with open(config_path) as f:
            return yaml.safe_load(f)
    return {}


def state_path_for(rom_filename: str) -> Path | None:
    """Resolve the fresh-boot savestate for `rom_filename` if one is committed.

    Returns None when no .state file exists — the harness treats this as
    "skip the per-scenario boot reset" so a new ROM works at session-scope
    until make_fresh_boot_state.py is run for it.
    """
    candidate = INTEGRATION_STATES_DIR / f"{Path(rom_filename).stem}.state"
    return candidate if candidate.exists() else None


def resolve_rom_path(rom_key: str) -> Path:
    """Resolve a registered rom_key to an absolute Path under config.yaml's rom.dir.

    Hard-fails (RuntimeError) on unknown key, missing rom.dir, or missing file.
    Per CLAUDE.md, hard-fail rather than pytest.skip so missing infra surfaces
    as a red test.
    """
    if rom_key not in ROM_REGISTRY:
        raise RuntimeError(
            f"unknown rom_key {rom_key!r}; known keys: {sorted(ROM_REGISTRY)}"
        )
    config = load_config()
    rom_dir_str = config.get("rom", {}).get("dir")
    if not rom_dir_str:
        raise RuntimeError(
            "rom.dir not configured in config.yaml; ROM-keyed test harness "
            f"cannot resolve {rom_key!r}"
        )
    rom_path = Path(rom_dir_str) / ROM_REGISTRY[rom_key]
    if not rom_path.exists():
        raise RuntimeError(
            f"ROM file not found for rom_key={rom_key!r}: expected "
            f"{rom_path} (filename {ROM_REGISTRY[rom_key]!r} under "
            f"rom.dir={rom_dir_str!r})"
        )
    return rom_path


def resolve_ra_paths(rom_key: str) -> tuple[Path, Path, Path]:
    """Resolve (retroarch_exe, ra_core_path, rom_path) for a given rom_key.

    Hard-fails if config.yaml is missing any of: emulator.retroarch_path,
    emulator.ra_core_path, or if either path does not exist on disk.
    Propagates `resolve_rom_path` failures.
    """
    config = load_config()
    emu = config.get("emulator", {})
    exe_str = emu.get("retroarch_path")
    core_str = emu.get("ra_core_path")
    if not exe_str:
        raise RuntimeError("emulator.retroarch_path not configured in config.yaml")
    if not core_str:
        raise RuntimeError("emulator.ra_core_path not configured in config.yaml")
    exe = Path(exe_str)
    core = Path(core_str)
    if not exe.exists():
        raise RuntimeError(f"retroarch_path does not exist on disk: {exe}")
    if not core.exists():
        raise RuntimeError(f"ra_core_path does not exist on disk: {core}")
    rom_path = resolve_rom_path(rom_key)
    return exe, core, rom_path
```

- [ ] **Step 2: Update unit tests to import from new module**

In `tests/unit/integration/test_ra_harness_factory.py`, replace every occurrence of:

```python
from tests.integration.conftest import _resolve_rom_path, ROM_REGISTRY
```

with:

```python
from tests.integration._rom_paths import resolve_rom_path, ROM_REGISTRY
```

and rename usages: `_resolve_rom_path(...)` → `resolve_rom_path(...)`, `_resolve_ra_paths(...)` → `resolve_ra_paths(...)`.

Also update the `patch()` targets: `patch("tests.integration.conftest._load_config", ...)` → `patch("tests.integration._rom_paths.load_config", ...)`.

- [ ] **Step 3: Update conftest to import from new module and remove the moved code**

In `tests/integration/conftest.py`:

Replace this block (lines roughly 36-87, 90-161 in the original file):

```python
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCENARIO_DIR = Path(__file__).resolve().parent / "scenarios"

pytestmark = pytest.mark.emulator

LOVE_YOURSELF_ROM_NAME = "Love Yourself.smc"
# ... ROM constants ...
# ... ROM_REGISTRY ...
# ... INTEGRATION_STATES_DIR ...
# ... _state_path_for ...
# ... _resolve_rom_path ...
# ... _resolve_ra_paths ...
# ... _load_config ...
```

with:

```python
from tests.integration._rom_paths import (
    CLEAN_SMW_ROM_NAME,
    INTEGRATION_STATES_DIR,
    LOVE_YOURSELF_GAME_ID,
    LOVE_YOURSELF_ROM_NAME,
    PROJECT_ROOT,
    ROM_REGISTRY,
    TOOTHPASTE_ROM_NAME,
    load_config,
    resolve_ra_paths,
    resolve_rom_path,
    state_path_for,
)

SCENARIO_DIR = Path(__file__).resolve().parent / "scenarios"

pytestmark = pytest.mark.emulator
```

Then update internal references inside conftest:
- `_state_path_for(...)` → `state_path_for(...)`
- `_resolve_rom_path(...)` → `resolve_rom_path(...)`
- `_resolve_ra_paths(...)` → `resolve_ra_paths(...)`
- `_load_config()` → `load_config()`

`LOVE_YOURSELF_GAME_ID` was at conftest.py:42 — moved to `_rom_paths.py`. Test_replay_fixture.py imports it from conftest, so leave the re-export visible (the import in conftest is sufficient — Python re-exports module-level names).

- [ ] **Step 4: Verify fast tests still pass**

```bash
python -m pytest tests/unit/integration/test_ra_harness_factory.py -v
python -m pytest -m "not emulator" -q
```

Expected: all passing.

- [ ] **Step 5: Type-check**

```bash
npx pyright tests/integration/_rom_paths.py tests/integration/conftest.py tests/unit/integration/test_ra_harness_factory.py
```

Expected: no new errors beyond the existing baseline.

- [ ] **Step 6: Commit**

```bash
git add tests/integration/_rom_paths.py tests/integration/conftest.py tests/unit/integration/test_ra_harness_factory.py
git commit -m "tests/integration: extract ROM registry + path resolvers to _rom_paths

Moves ROM_REGISTRY, resolve_rom_path, resolve_ra_paths, load_config,
state_path_for from conftest.py to tests/integration/_rom_paths.py so
tests/unit/integration/test_ra_harness_factory.py can import a real
module instead of reaching into private conftest symbols. Public
names drop the underscore prefix (the module name itself stays
underscore-prefixed as test-internal)."
```

---

## Task 3: Move diagnostics machinery to `_diagnostics.py`

**Files:**
- Create: `tests/integration/_diagnostics.py`
- Modify: `tests/integration/conftest.py` (remove diagnostic functions, hooks delegate to imported names, call `install_log_handler()` once at module load)
- Modify: `tests/unit/integration/test_diagnostic_hook.py` (update imports + monkeypatch targets)

- [ ] **Step 1: Write the new module**

```python
# tests/integration/_diagnostics.py
"""Diagnostic capture for integration test failures.

Owns the ring-buffer logging handler (collects recent spinlab log lines)
and the formatters / collectors that the pytest hooks in conftest.py call
on failure. Pulled out of conftest so tests/unit/integration/ can import
real names rather than private conftest symbols.
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

import requests as http_requests

if TYPE_CHECKING:
    import pytest
    from tests.integration.ra_harness import RAHarnessLaunchError

# Tail counts. 30 is plenty to capture the RA boot sequence (~10 lines)
# plus any "core failed to load" + crash spew, without burying the report
# under thousands of frame-tick lines.
HARNESS_LOG_TAIL_LINES = 30
RING_TAIL_LINES = 30
EVENT_LOG_CAPACITY = 200


class RingHandler(logging.Handler):
    """Fixed-capacity ring buffer logging handler."""

    def __init__(self, capacity: int = EVENT_LOG_CAPACITY):
        super().__init__()
        self._buf: list[str] = []
        self._capacity = capacity

    def emit(self, record: logging.LogRecord) -> None:
        line = self.format(record)
        self._buf.append(line)
        if len(self._buf) > self._capacity:
            self._buf = self._buf[-self._capacity:]

    def recent(self, n: int = 30) -> list[str]:
        return self._buf[-n:]

    def clear(self) -> None:
        self._buf = []


# Module-level singleton. Installed onto the spinlab logger by `install_log_handler`.
ring = RingHandler()
ring.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))

_installed = False


def install_log_handler() -> None:
    """Attach `ring` to the spinlab logger exactly once per process.

    Called by `tests/integration/conftest.py` at module load. Idempotent —
    repeat calls are no-ops, so importing the module from unit tests
    doesn't double-register.
    """
    global _installed
    if _installed:
        return
    logging.getLogger("spinlab").addHandler(ring)
    _installed = True


def format_pause_toggle_failure(harness, exc: Exception) -> str:
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


def format_dashboard_startup_failure(
    *,
    port: int,
    attempts: int,
    interval_s: float,
    last_error: Exception | None,
    subject: str = "Fake dashboard server",
) -> str:
    """Format a dashboard startup timeout message.

    Names the bound port, elapsed wall time, and most recent error so the
    operator can tell port-occupied apart from a panicked dashboard.
    """
    elapsed = attempts * interval_s
    err_str = (
        f"{type(last_error).__name__}: {last_error}"
        if last_error else "no error captured"
    )
    return (
        f"{subject} did not start on port {port} within "
        f"{elapsed:.1f}s ({attempts} × {interval_s}s). Last error: {err_str}"
    )


def collect_diagnostics(item: "pytest.Item") -> str:
    """Best-effort snapshot of integration test state at failure time.

    Walks `item.funcargs`:
      - For tuples shaped `(str_url, Database, ...)`, emits /api/state +
        DB-counts block.
      - For objects exposing `.proc` and `.client`, emits a harness block
        with pid / port / proc.poll() and a tail of the per-launch retroarch.log.

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
                        tail = text.splitlines()[-HARNESS_LOG_TAIL_LINES:]
                        if tail:
                            parts.append(f"  retroarch.log tail ({len(tail)} lines):")
                            for line in tail:
                                parts.append(f"    {line}")
                except Exception as exc:
                    parts.append(f"  retroarch.log: <unavailable: {exc}>")

    recent = ring.recent(RING_TAIL_LINES)
    if recent:
        parts.append(f"  Recent spinlab log ({len(recent)} lines):")
        for line in recent:
            parts.append(f"    {line}")

    if not parts:
        return ""
    return "\n--- SpinLab Integration Diagnostics ---\n" + "\n".join(parts)


def collect_launch_failure_diagnostics(exc: "RAHarnessLaunchError") -> str:
    """Best-effort snapshot when RAHarness.launch fails during fixture setup.

    Reads structured fields off the typed exception and tails the preserved
    retroarch.log if its `log_path` still exists. Always tails the spinlab
    logger ring at the end so the report has parity with the call-phase block.
    """
    parts: list[str] = [
        f"  RAHarnessLaunchError:"
        f" stage={exc.stage!r}"
        f" pid={exc.pid}"
        f" port={exc.port}"
        f" startup_duration_s={exc.startup_duration_s}",
    ]
    log_path = exc.log_path
    if log_path is not None:
        try:
            if log_path.exists():
                text = log_path.read_text(errors="replace")
                tail = text.splitlines()[-HARNESS_LOG_TAIL_LINES:]
                if tail:
                    parts.append(
                        f"  retroarch.log tail ({len(tail)} lines) from {log_path}:"
                    )
                    for line in tail:
                        parts.append(f"    {line}")
        except Exception as inner:
            parts.append(f"  retroarch.log: <unavailable: {inner}>")

    recent = ring.recent(RING_TAIL_LINES)
    if recent:
        parts.append(f"  Recent spinlab log ({len(recent)} lines):")
        for line in recent:
            parts.append(f"    {line}")

    return "\n--- SpinLab Launch-Failure Diagnostics ---\n" + "\n".join(parts)
```

- [ ] **Step 2: Update conftest.py — remove moved code, call install_log_handler, delegate hooks**

Delete from `tests/integration/conftest.py`:
- `_EVENT_LOG_CAPACITY = 200` constant
- `class _RingHandler(...)` block
- `_ring = _RingHandler()` + `_ring.setFormatter(...)` + `logging.getLogger("spinlab").addHandler(_ring)` lines
- `_format_pause_toggle_failure` function
- `_format_dashboard_startup_failure` function
- `_HARNESS_LOG_TAIL_LINES`, `_RING_TAIL_LINES` constants
- `_collect_diagnostics` function
- `_collect_launch_failure_diagnostics` function

Add at the top (after other imports):

```python
from tests.integration._diagnostics import (
    collect_diagnostics,
    collect_launch_failure_diagnostics,
    format_dashboard_startup_failure,
    format_pause_toggle_failure,
    install_log_handler,
    ring,
)

install_log_handler()
```

Update the call sites still in conftest:
- `_format_pause_toggle_failure(harness, exc)` → `format_pause_toggle_failure(harness, exc)`
- `_format_dashboard_startup_failure(...)` → `format_dashboard_startup_failure(...)`
- `_ring.clear()` (in `pytest_runtest_setup`) → `ring.clear()`
- In `pytest_runtest_makereport`, the `_collect_diagnostics(item)` call becomes `collect_diagnostics(item)` and `_collect_launch_failure_diagnostics(exc)` becomes `collect_launch_failure_diagnostics(exc)`.

- [ ] **Step 3: Update `test_diagnostic_hook.py` to import from `_diagnostics`**

```python
# tests/unit/integration/test_diagnostic_hook.py — top imports
from tests.integration._diagnostics import collect_diagnostics
```

Update every other `from tests.integration.conftest import _collect_...` / `_format_...` to `from tests.integration._diagnostics import collect_..., format_...`.

Update the monkeypatch target on line 31:

```python
monkeypatch.setattr(
    "tests.integration._diagnostics.http_requests.get",
    lambda url, timeout=2: fake_resp,
)
```

- [ ] **Step 4: Run the affected unit tests**

```bash
python -m pytest tests/unit/integration/test_diagnostic_hook.py -v
```

Expected: all passing.

- [ ] **Step 5: Run full fast suite + type-check**

```bash
python -m pytest -m "not emulator" -q
npx pyright tests/integration/_diagnostics.py tests/integration/conftest.py tests/unit/integration/test_diagnostic_hook.py
```

Expected: 886 passed (or current baseline + new tests), 0 new pyright errors.

- [ ] **Step 6: Commit**

```bash
git add tests/integration/_diagnostics.py tests/integration/conftest.py tests/unit/integration/test_diagnostic_hook.py
git commit -m "tests/integration: extract diagnostics machinery to _diagnostics

Moves RingHandler, ring instance, collect_diagnostics,
collect_launch_failure_diagnostics, format_pause_toggle_failure,
format_dashboard_startup_failure out of conftest.py into a real
module that unit tests can import directly. Conftest calls
install_log_handler() once at module load (idempotent)."
```

---

## Task 4: Move harness factory to `_harness_factory.py`

**Files:**
- Create: `tests/integration/_harness_factory.py`
- Modify: `tests/integration/conftest.py` (remove HarnessFactory code, import from new module)
- Modify: `tests/unit/integration/test_ra_harness_factory.py` (update imports for `_HarnessFactory` and `_harness_factory_impl`)

- [ ] **Step 1: Write the new module**

```python
# tests/integration/_harness_factory.py
"""Session-scoped RAHarness factory used by integration fixtures.

Cache key is `(rom_key, use_fresh_state)` so a single test session can hold
both a fresh-state-isolated and a no-reset harness for the same ROM.
"""
from __future__ import annotations

import logging

from tests.integration._rom_paths import (
    ROM_REGISTRY,
    resolve_ra_paths,
    state_path_for,
)
from tests.integration.ra_harness import (
    RAHarness,
    RAHarnessLaunchError,
)


def _free_udp_port() -> int:
    """Find a free UDP port.

    Small TOCTOU window between the bind here releasing and RetroArch binding
    to the same port — acceptable because the harness's NCI ping retries cover
    transient failures, and the loopback UDP port space is otherwise quiet on
    a test host.
    """
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class HarnessFactory:
    """Session-scoped cache mapping (rom_key, use_fresh_state) -> RAHarness.

    Separated from the pytest fixture so unit tests can drive the cache and
    teardown logic without a real fixture lifecycle.
    """

    def __init__(self) -> None:
        self._cache: dict[tuple[str, bool], RAHarness] = {}

    def __call__(self, rom_key: str, use_fresh_state: bool = True) -> RAHarness:
        """Return (or create + cache) a harness for `rom_key`.

        `use_fresh_state=True` (the default) wires a per-launch isolated
        savestate_directory with the fresh-boot state pre-staged at
        FRESH_BOOT_STATE_SLOT, and causes RAPokeEngine to load it before
        each scenario. Required by the poke-transition tests.

        `use_fresh_state=False` is for fixtures whose RA process must talk
        to the user's actual savestate_directory — currently just the
        replay fixture.
        """
        cache_key = (rom_key, use_fresh_state)
        if cache_key in self._cache:
            return self._cache[cache_key]
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

    def teardown_all(self) -> None:
        while self._cache:
            cache_key, harness = self._cache.popitem()
            try:
                harness.teardown()
            except Exception:
                logging.getLogger(__name__).exception(
                    "ra_harness teardown failed for %r", cache_key
                )


def harness_factory_impl() -> HarnessFactory:
    """Factory constructor surface used by both the pytest fixture and unit tests."""
    return HarnessFactory()
```

- [ ] **Step 2: Update conftest.py — import + remove moved code**

Delete from `tests/integration/conftest.py`:
- `class _HarnessFactory:` block (the entire class)
- `def _harness_factory_impl()` function
- `def _free_udp_port()` function (now lives in `_harness_factory.py`)

Add at the top:

```python
from tests.integration._harness_factory import (
    HarnessFactory,
    harness_factory_impl,
)
```

Update the `ra_harness_factory` fixture body:

```python
@pytest.fixture(scope="session")
def ra_harness_factory():
    """Session-scoped factory: factory(rom_key) -> RAHarness, cached per rom_key.

    Hard-fails (RuntimeError) on any missing infrastructure — no pytest.skip.
    See ROM_REGISTRY for the available rom_keys.
    """
    factory = harness_factory_impl()
    yield factory
    factory.teardown_all()
```

(Same body — just the function call name changed.)

- [ ] **Step 3: Update `test_ra_harness_factory.py` imports**

Replace each:

```python
from tests.integration.conftest import _harness_factory_impl
from tests.integration.conftest import _HarnessFactory
```

with:

```python
from tests.integration._harness_factory import harness_factory_impl
from tests.integration._harness_factory import HarnessFactory
```

Rename usages: `_harness_factory_impl()` → `harness_factory_impl()`, `_HarnessFactory` → `HarnessFactory`.

- [ ] **Step 4: Run affected tests**

```bash
python -m pytest tests/unit/integration/test_ra_harness_factory.py -v
python -m pytest -m "not emulator" -q
```

Expected: all passing.

- [ ] **Step 5: Type-check**

```bash
npx pyright tests/integration/_harness_factory.py tests/integration/conftest.py tests/unit/integration/test_ra_harness_factory.py
```

Expected: no new errors.

- [ ] **Step 6: Commit**

```bash
git add tests/integration/_harness_factory.py tests/integration/conftest.py tests/unit/integration/test_ra_harness_factory.py
git commit -m "tests/integration: extract HarnessFactory to _harness_factory

Moves the session-scoped (rom_key, use_fresh_state) cache and the
factory_impl constructor out of conftest into a real module. Drops
the underscore prefixes on the public names (HarnessFactory,
harness_factory_impl)."
```

---

## Task 5: `DashboardHarness` context manager (TDD)

The dashboard-startup duplication (A1) lives in `fake_dashboard_server` (188-258) and `replay_ra_dashboard` (431-546). Both: create tmpdir → Database → AppConfig → uvicorn thread → wait for `/api/state` → tear down on exit. They differ in:
- `fake_dashboard_server` uses FakeEmuBackend, no rom_dir, no NCI port.
- `replay_ra_dashboard` uses real RA backend, real savestate_dir, NCI port from harness.

Plan: one `DashboardHarness` class that owns the lifecycle; takes an `AppConfig` from the caller (so each fixture composes its own config), optionally swaps the backend for a FakeEmuBackend after `create_app`.

**Files:**
- Create: `tests/integration/_dashboard_harness.py`
- Test: `tests/unit/integration/test_dashboard_harness.py` (new)

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/integration/test_dashboard_harness.py
"""Tests for DashboardHarness — verifies the context-manager lifecycle without
booting a real emulator (the fake_emu_backend=True path)."""
from __future__ import annotations

import pytest
import requests

from tests.integration._dashboard_harness import DashboardHarness


def test_dashboard_harness_starts_and_serves_api_state(tmp_path):
    """In fake-emu mode, the harness should bring up a dashboard whose
    /api/state returns 200."""
    with DashboardHarness.fake(tmp_path_root=tmp_path) as ctx:
        resp = requests.get(f"{ctx.base_url}/api/state", timeout=2)
        assert resp.status_code == 200
        # /api/state always reports emu_connected from session.emu;
        # FakeEmuBackend starts connected=True.
        body = resp.json()
        assert body.get("emu_connected") is True


def test_dashboard_harness_tears_down_cleanly(tmp_path):
    """After exit, the port should be free and tmp dir gone."""
    with DashboardHarness.fake(tmp_path_root=tmp_path) as ctx:
        port = ctx.base_url.split(":")[-1]
        tmp = ctx.tmp_path
        assert tmp.exists()
    # uvicorn join should have completed
    assert not tmp.exists() or not any(tmp.iterdir())  # rmtree ignore_errors=True


def test_dashboard_harness_exposes_db_and_session(tmp_path):
    """Test bodies use db and session for direct manipulation."""
    with DashboardHarness.fake(tmp_path_root=tmp_path) as ctx:
        # db is a real Database; session is the SessionManager from the FastAPI app.
        assert ctx.db is not None
        assert ctx.session is not None
        # Sanity: db is queryable
        cur = ctx.db.conn.execute("SELECT COUNT(*) FROM segments")
        assert cur.fetchone()[0] == 0


def test_dashboard_harness_fail_to_start_raises_with_outcome(tmp_path, monkeypatch):
    """If the dashboard never reports 200, we get a TimeoutError naming the
    operation. (Smoke check; full path is exercised by the fixtures.)"""
    # Force a startup timeout by patching the wait helper to always fail.
    from tests.integration import _dashboard_harness as dh
    from tests.integration._wait_for import WaitOutcome

    def fake_wait(**_kwargs):
        return WaitOutcome(
            succeeded=False, name="dashboard_ready",
            elapsed_s=0.5, attempts=2, last_reason="status 500",
        )

    monkeypatch.setattr(dh, "wait_for", fake_wait)

    with pytest.raises(RuntimeError, match="dashboard_ready"):
        with DashboardHarness.fake(tmp_path_root=tmp_path):
            pass
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/unit/integration/test_dashboard_harness.py -v
```

Expected: FAIL with `ModuleNotFoundError: tests.integration._dashboard_harness`.

- [ ] **Step 3: Implement `_dashboard_harness.py`**

```python
# tests/integration/_dashboard_harness.py
"""DashboardHarness — context manager that owns the lifecycle of a FastAPI
dashboard wired to an in-process backend, for use by integration fixtures
and unit tests that need a real HTTP surface.

Replaces the two near-identical tmpdir+uvicorn+wait-for-ready blocks in
`fake_dashboard_server` (FakeEmuBackend) and `replay_ra_dashboard` (real
RA backend) — callers pass an AppConfig and an optional fake_emu flag.
"""
from __future__ import annotations

import shutil
import socket
import tempfile
import threading
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests as http_requests
import uvicorn

from spinlab.config import AppConfig, EmulatorConfig, NetworkConfig
from spinlab.db import Database

from tests.integration._wait_for import WaitOutcome, wait_for


def _free_port() -> int:
    """Find a free TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class DashboardContext:
    """What a `with DashboardHarness(...) as ctx:` block sees."""

    base_url: str
    db: Database
    session: object  # SessionManager — typed as object to avoid an import cycle
    tmp_path: Path


def _status_200(resp: http_requests.Response) -> tuple[bool, str]:
    if resp.status_code == 200:
        return True, ""
    return False, f"HTTP {resp.status_code}"


class DashboardHarness(AbstractContextManager):
    """Owns the tmpdir + Database + uvicorn-thread lifecycle for a dashboard.

    Two construction paths:
      - `DashboardHarness(config=..., fake_emu=False)` — caller-supplied
        AppConfig. The dashboard's event_loop will try to connect to
        whatever backend the config points at.
      - `DashboardHarness.fake(tmp_path_root=...)` — builds an AppConfig
        with throwaway ports and swaps in a FakeEmuBackend. The event_loop
        keeps failing to connect to nothing, which is fine for HTTP-only
        contract tests.
    """

    def __init__(
        self,
        *,
        config: AppConfig,
        fake_emu: bool = False,
        tmp_path: Path,
        startup_timeout_s: float = 10.0,
    ) -> None:
        self._config = config
        self._fake_emu = fake_emu
        self._tmp_path = tmp_path
        self._startup_timeout_s = startup_timeout_s
        self._server: Optional[uvicorn.Server] = None
        self._thread: Optional[threading.Thread] = None
        self._db: Optional[Database] = None
        self._app = None

    @classmethod
    def fake(
        cls, *, tmp_path_root: Path, startup_timeout_s: float = 10.0,
    ) -> "DashboardHarness":
        """Construct a fake-backed harness with throwaway config.

        The dashboard port is picked from the free range. NetworkConfig.port
        is a free port that nothing will bind to, so the event_loop's
        connect-retries fail fast.
        """
        tmp = Path(tempfile.mkdtemp(prefix="spinlab_fake_", dir=tmp_path_root))
        dashboard_port = _free_port()
        fake_tcp_port = _free_port()
        config = AppConfig(
            network=NetworkConfig(
                host="127.0.0.1",
                port=fake_tcp_port,
                dashboard_port=dashboard_port,
            ),
            emulator=EmulatorConfig(
                savestate_dir=tmp / "ra",
                spinlab_state_dir=tmp / "sl",
            ),
            data_dir=tmp,
            rom_dir=None,
        )
        return cls(
            config=config, fake_emu=True, tmp_path=tmp,
            startup_timeout_s=startup_timeout_s,
        )

    def __enter__(self) -> DashboardContext:
        from spinlab.dashboard import create_app

        self._db = Database(str(self._tmp_path / "spinlab.db"))
        self._app = create_app(db=self._db, config=self._config)

        if self._fake_emu:
            from tests.conftest import FakeEmuBackend
            fake_emu_backend = FakeEmuBackend(connected=True)
            self._app.state.session.emu = fake_emu_backend
            self._app.state.session.capture.emu = fake_emu_backend
            self._app.state.session.cold_fill.emu = fake_emu_backend

        uvi_config = uvicorn.Config(
            self._app,
            host="127.0.0.1",
            port=self._config.network.dashboard_port,
            log_level="warning",
        )
        self._server = uvicorn.Server(uvi_config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()

        base_url = f"http://127.0.0.1:{self._config.network.dashboard_port}"

        outcome = wait_for(
            name="dashboard_ready",
            fetch=lambda: http_requests.get(f"{base_url}/api/state", timeout=1.0),
            predicate=_status_200,
            timeout_s=self._startup_timeout_s,
            interval_s=0.25,
        )
        if not outcome.succeeded:
            self._teardown()
            raise RuntimeError(outcome.format_message())

        return DashboardContext(
            base_url=base_url,
            db=self._db,
            session=self._app.state.session,
            tmp_path=self._tmp_path,
        )

    def __exit__(self, exc_type, exc, tb) -> None:
        self._teardown()

    def _teardown(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)
        if self._db is not None:
            self._db.close()
        shutil.rmtree(self._tmp_path, ignore_errors=True)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/unit/integration/test_dashboard_harness.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Type-check**

```bash
npx pyright tests/integration/_dashboard_harness.py tests/unit/integration/test_dashboard_harness.py
```

Expected: no new errors.

- [ ] **Step 6: Commit**

```bash
git add tests/integration/_dashboard_harness.py tests/unit/integration/test_dashboard_harness.py
git commit -m "tests/integration: add DashboardHarness context manager

Wraps tmpdir + Database + uvicorn-thread lifecycle for a dashboard
wired to a chosen backend. Two construction paths: DashboardHarness(
config=...) for real-backend tests and DashboardHarness.fake() for
HTTP-only contract tests. Uses wait_for() for startup timeout so
failures name 'dashboard_ready'. Fixtures will be converted in
later tasks."
```

---

## Task 6: Convert `fake_dashboard_server` to use `DashboardHarness`

**Files:**
- Modify: `tests/integration/conftest.py:188-258` (collapse to ~30 lines via DashboardHarness)

- [ ] **Step 1: Rewrite `fake_dashboard_server`**

Replace the entire `fake_dashboard_server` fixture body in `tests/integration/conftest.py` (~lines 188-258) with:

```python
@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def fake_dashboard_server(tmp_path_factory):
    """Start a FastAPI dashboard with a FakeEmuBackend — no live emulator required.

    Mirrors the real ``dashboard_server`` fixture but swaps ``session.emu`` for
    the in-process FakeEmuBackend (see tests/conftest.py) so tests can exercise
    the dashboard's HTTP API and SessionManager without booting an emulator.

    The dashboard's background event_loop still runs and keeps trying to open
    a backend connection on the configured port — nothing listens there, so each
    attempt fails fast and the loop sleeps. The session's ``emu`` reference is
    the fake, which ``SystemState`` reads for ``emu_connected``.

    Yields (base_url, db, session).
    """
    from tests.integration._dashboard_harness import DashboardHarness

    tmp_root = tmp_path_factory.mktemp("fake_dashboard")
    with DashboardHarness.fake(tmp_path_root=tmp_root) as ctx:
        yield ctx.base_url, ctx.db, ctx.session
```

This deletes ~50 lines of duplicated setup code.

- [ ] **Step 2: Run the affected tests**

```bash
python -m pytest tests/integration/test_frontend_smoke.py -m emulator -v
python -m pytest -m "not emulator" -q
```

The frontend smoke is the primary consumer of `fake_dashboard_server` (via `fake_game_loaded`). It's marked `emulator` only because it shares the integration conftest's `pytestmark`; in practice it does not need a live emulator.

Expected: all consumers green.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/conftest.py
git commit -m "tests/integration: collapse fake_dashboard_server onto DashboardHarness

~50 lines of dashboard-startup boilerplate replaced by a single
DashboardHarness.fake() context. Behaviour unchanged: same tmpdir,
same FakeEmuBackend swap, same /api/state startup wait."
```

---

## Task 7: Convert `replay_ra_dashboard` to use `DashboardHarness` + `wait_for`

The real-RA fixture is trickier than `fake_dashboard_server` because it must:
1. Skip if `emulator.savestate_dir` is not configured (legitimate user-config gate).
2. Compose AppConfig from real config.yaml values + the harness's NCI port.
3. After dashboard startup, wait a second time for the orchestrator to connect to RA (emu_connected + game_id populated).
4. Unpause RA via direct NCI call.
5. Tear down even if pause_toggle fails mid-setup.

The DashboardHarness gives us (1)/(2)/(3a — initial startup). The orchestrator-ready wait and the pause_toggle stay in the fixture.

**Files:**
- Modify: `tests/integration/conftest.py:431-546` (collapse the dashboard-startup half; keep orchestrator-ready and pause_toggle logic)

- [ ] **Step 1: Rewrite `replay_ra_dashboard`**

Replace the body of `replay_ra_dashboard` (~lines 431-546) with:

```python
@pytest.fixture(scope="session")
def replay_ra_dashboard(ra_harness_love_yourself_no_reset, tmp_path_factory):
    """Start a dashboard pointed at the Love Yourself RA session for replay tests.

    Mirrors ``replay_dashboard`` but uses the RA backend (build_orchestrator)
    instead of the legacy Mesen+TCP backend. The RA process is already up
    (ra_harness_love_yourself); this fixture wires the dashboard to it via
    NCI at the configured port.

    Phase E PLAY_REPLAY requires RA to be in PLAYING (not PAUSED) state.
    The harness leaves RA paused; we unpause it here so the orchestrator's
    _on_replay → MoviePlayer.play → play_replay() works correctly.

    Yields (base_url, db, tmp_path) — tmp_path is the data dir where the
    test should stage its fixture files.
    """
    from tests.integration._dashboard_harness import DashboardHarness

    config_raw = load_config()
    emu_raw = config_raw.get("emulator", {})

    savestate_dir_str = emu_raw.get("savestate_dir")
    ra_core_subdir = emu_raw.get("ra_core_subdir") or "Snes9x"

    if not savestate_dir_str:
        pytest.skip("replay_ra_dashboard: emulator.savestate_dir not configured")

    savestate_dir = Path(savestate_dir_str)
    tmp_root = tmp_path_factory.mktemp("spinlab_ra_replay")
    tmp_path = Path(tempfile.mkdtemp(prefix="spinlab_ra_replay_", dir=tmp_root))
    spinlab_state_dir = tmp_path / "spinlab_states"
    spinlab_state_dir.mkdir(parents=True, exist_ok=True)

    dashboard_port = _free_port()
    rom_dir = resolve_rom_path("love_yourself").parent

    from spinlab.config import AppConfig, EmulatorConfig, NetworkConfig
    config = AppConfig(
        network=NetworkConfig(
            host="127.0.0.1",
            port=15482,  # unused — RA backend uses NCI, not TCP
            dashboard_port=dashboard_port,
            nci_port=ra_harness_love_yourself_no_reset.client.port,
        ),
        emulator=EmulatorConfig(
            savestate_dir=savestate_dir,
            spinlab_state_dir=spinlab_state_dir,
            ra_core_subdir=ra_core_subdir,
        ),
        data_dir=tmp_path,
        rom_dir=rom_dir,
    )

    harness_cm = DashboardHarness(
        config=config, fake_emu=False, tmp_path=tmp_path,
    )
    try:
        ctx = harness_cm.__enter__()
    except RuntimeError as exc:
        # DashboardHarness raises with the WaitOutcome.format_message() text;
        # surface it as a pytest.fail so the report shape matches the old path.
        pytest.fail(str(exc))

    try:
        # Wait for the orchestrator to connect to RA and receive rom_info so the
        # dashboard has a game_id (required before /api/replay/start will resolve).
        from tests.integration._wait_for import wait_for

        def _fetch_state():
            return http_requests.get(
                f"{ctx.base_url}/api/state", timeout=1.0,
            ).json()

        def _orchestrator_ready(state):
            if state.get("emu_connected") and state.get("game_id"):
                return True, ""
            return False, (
                f"emu_connected={state.get('emu_connected')!r} "
                f"game_id={state.get('game_id')!r}"
            )

        outcome = wait_for(
            name="orchestrator_ready",
            fetch=_fetch_state,
            predicate=_orchestrator_ready,
            timeout_s=10.0,
            interval_s=0.25,
        )
        if not outcome.succeeded:
            pytest.fail(outcome.format_message())

        # PLAY_REPLAY requires RA in PLAYING state. The harness left it paused.
        harness = ra_harness_love_yourself_no_reset
        try:
            status = harness.client.get_status()
            if status.state == "PAUSED":
                harness.client.pause_toggle()
                time.sleep(0.3)  # let RA settle into PLAYING before tests POST replay/start
        except Exception as exc:
            pytest.fail(format_pause_toggle_failure(harness, exc))

        yield ctx.base_url, ctx.db, ctx.tmp_path
    finally:
        harness_cm.__exit__(None, None, None)
```

This:
- Replaces the dashboard-startup block (lines ~462-503) with a single `DashboardHarness(...)` construction + `__enter__`.
- Uses the new `wait_for()` for the orchestrator-ready check — the timeout message now names `"orchestrator_ready"`.
- Keeps the pause_toggle logic and the `format_pause_toggle_failure` helper.
- Wraps the body in try/finally so teardown always runs.

Also delete now-unused helpers from conftest:
- `_wait_for_dashboard_state` — replaced by `wait_for`
- `_status_200` — moved into `_dashboard_harness.py`
- `_teardown_replay_dashboard` — replaced by `DashboardHarness.__exit__`

- [ ] **Step 2: Verify import surface still works**

The fixture imports `Path` and `tempfile` and `time` — confirm they're already in conftest.py's import block at the top. Also confirm `http_requests` (the `requests` module alias) is imported in conftest.

- [ ] **Step 3: Run the emulator suite to catch any regression**

```bash
python -m pytest -m emulator -v
```

Expected: all 12 emulator tests pass. The `test_replay_fixture.py` is the canary for `replay_ra_dashboard` — it must still go green end-to-end.

- [ ] **Step 4: Run fast suite as a sanity check**

```bash
python -m pytest -m "not emulator" -q
```

Expected: 886 passed (or current baseline + the new helper tests).

- [ ] **Step 5: Type-check**

```bash
npx pyright tests/integration/conftest.py
```

Expected: no new errors.

- [ ] **Step 6: Commit**

```bash
git add tests/integration/conftest.py
git commit -m "tests/integration: collapse replay_ra_dashboard onto DashboardHarness

Dashboard startup boilerplate (~70 lines) replaced by a single
DashboardHarness(config=...) context. Orchestrator-ready wait now
uses the typed wait_for() helper so the timeout names
'orchestrator_ready' instead of 'Fake dashboard server did not
start'. Behaviour unchanged: same skip on missing savestate_dir,
same pause_toggle logic, same teardown order.

Also drops the now-unused _wait_for_dashboard_state, _status_200,
and _teardown_replay_dashboard helpers from conftest."
```

---

## Task 8: Convert `run_scenario` fixture to use `wait_for`

The current `run_scenario` wraps `engine.run_scenario` in `asyncio.wait_for(timeout=30.0)` and the resulting TimeoutError has no scenario name (OB1).

Fix: catch the `asyncio.TimeoutError` and re-raise with the scenario name + a timeout message that mirrors `WaitOutcome.format_message()` shape.

**Files:**
- Modify: `tests/integration/conftest.py:406-428` (`run_scenario` fixture)

- [ ] **Step 1: Rewrite `run_scenario`**

```python
@pytest.fixture
def run_scenario(ra_harness_love_yourself):
    """Send a poke scenario through the Love Yourself RA harness and collect events.

    Pinned to Love Yourself because that's the ROM whose committed fresh-boot
    savestate lands the player in a level (vanilla SMW's lands on the title
    screen — see ra_harness_vanilla_smw).
    """

    async def _run(scenario_name: str, timeout: float = 30.0) -> list:
        scenario_path = SCENARIO_DIR / scenario_name
        if not scenario_path.exists():
            pytest.fail(f"Scenario file not found: {scenario_path}")
        scenario = parse_poke_file(str(scenario_path))
        start = time.monotonic()
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(
                    ra_harness_love_yourself.engine.run_scenario, scenario
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            elapsed = time.monotonic() - start
            pytest.fail(
                f"run_scenario({scenario_name!r}) timed out after "
                f"{elapsed:.1f}s (limit {timeout:.1f}s)"
            )

    return _run
```

This:
- Preserves the same external API (`await run_scenario("foo.poke")`).
- On timeout, raises `pytest.fail` with the scenario name + elapsed time so the operator can tell which scenario hung.

(We don't use `wait_for()` here because the underlying primitive is
`asyncio.to_thread` + `asyncio.wait_for`, which is structurally different
from the polling helpers. The naming improvement is purely the error message.)

- [ ] **Step 2: Run the emulator suite**

```bash
python -m pytest -m emulator -v
```

Expected: pass; the change is observable only on timeout.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/conftest.py
git commit -m "tests/integration: name the scenario in run_scenario timeout

Catches asyncio.TimeoutError from wait_for and re-raises with the
scenario name + elapsed time. Previously, a 30s timeout produced
a bare TimeoutError with no indication of which scenario hung."
```

---

## Task 9: Rewrite `_wait_for_replay_mode` in `test_replay_fixture.py`

The helper at lines 38-52 of `tests/integration/test_replay_fixture.py` conflates two failure modes ("mode never reached replay" vs "mode=replay but frame_count is 0") into one message dump.

Fix: use the new `wait_for()` with a predicate that names which condition is currently unmet.

**Files:**
- Modify: `tests/integration/test_replay_fixture.py:38-52`

- [ ] **Step 1: Rewrite the helper**

Replace the existing `_wait_for_replay_mode` with:

```python
def _wait_for_replay_mode(base_url: str, timeout: float = 15.0) -> dict:
    """Wait until mode is 'replay' AND replay_started has set a nonzero frame total."""
    from tests.integration._wait_for import wait_for

    def _fetch():
        return _api(base_url, "get", "/api/state").json()

    def _predicate(state):
        mode = state.get("mode")
        if mode != "replay":
            return False, f"mode={mode!r} (waiting for 'replay')"
        replay = state.get("replay")
        if not replay or not replay.get("total", 0) > 0:
            total = replay.get("total") if replay else None
            return False, f"mode='replay' but replay.total={total!r} (waiting for nonzero)"
        return True, ""

    outcome = wait_for(
        name="replay_mode_with_frame_total",
        fetch=_fetch,
        predicate=_predicate,
        timeout_s=timeout,
        interval_s=POLL_INTERVAL_S,
    )
    if not outcome.succeeded:
        pytest.fail(outcome.format_message())
    return _fetch()
```

The new error message will be either:
- `wait_for(replay_mode_with_frame_total) timed out after N attempt(s) in T.Ts; last reason: mode='idle' (waiting for 'replay')`
- `wait_for(replay_mode_with_frame_total) timed out after N attempt(s) in T.Ts; last reason: mode='replay' but replay.total=0 (waiting for nonzero)`

— each one disambiguates the failure mode.

- [ ] **Step 2: Run the replay fixture test**

```bash
python -m pytest tests/integration/test_replay_fixture.py -v
```

Expected: pass (the change only affects the error message; happy path is identical).

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_replay_fixture.py
git commit -m "tests/integration/test_replay_fixture: name replay-wait predicate

_wait_for_replay_mode now uses wait_for() so the timeout message
disambiguates 'mode never advanced to replay' from 'mode=replay
but replay.total is 0' — previously both produced the same
'Last state: <dump>' message."
```

---

## Task 10: Final verification + scan-file update

**Files:**
- Modify: `docs/superpowers/scans/2026-05-17-improve-2220.md` (update Picked section to mark CF-1 as shipped)
- Modify: `tests/integration/conftest.py` (final cleanup pass — remove any now-unused imports)

- [ ] **Step 1: Run the full test suite**

```bash
python -m pytest
```

Per CLAUDE.md, this is the unfiltered suite (unit + emulator + frontend). All must pass before this work is considered done. Skips count as failures.

Expected: 886+ passed, 0 failed, 0 unexpected skips.

- [ ] **Step 2: Type-check the whole tree**

```bash
npx pyright tests/integration/ tests/unit/integration/
```

Expected: no new errors introduced beyond the existing baseline.

- [ ] **Step 3: Confirm conftest shrank significantly**

```bash
wc -l tests/integration/conftest.py
```

Expected: ~400 lines (down from 871). If the file is still >500 lines, walk through it once more — there should be no remaining duplicated dashboard-startup code, no in-file diagnostic formatters, no in-file ROM resolvers.

- [ ] **Step 4: Remove any now-unused imports from conftest**

Run pyright with unused-import reporting:

```bash
npx pyright --outputjson tests/integration/conftest.py | python -c "import sys, json; d=json.load(sys.stdin); print('\n'.join(diag['message'] for diag in d.get('generalDiagnostics', []) if 'unused' in diag.get('message','').lower()))"
```

Delete any imports that became unused after the extractions (likely: `socket`, `logging`, `requests`, `uvicorn`, `tempfile`, `shutil` if they no longer appear in conftest's body). Keep `time` (used by `replay_ra_dashboard` for the `time.sleep(0.3)` after pause_toggle).

- [ ] **Step 5: Update the scan file**

In `docs/superpowers/scans/2026-05-17-improve-2220.md`, find the line:

```
- **CF-1** (medium, high-leverage) → handed off to `superpowers:writing-plans` on the same branch
```

Replace with:

```
- **CF-1** (medium, high-leverage) → shipped on branch `improve/test-infra-helpers-and-typed-fixtures`. Conftest.py: 871 → ~400 lines. New modules: `_wait_for.py`, `_rom_paths.py`, `_diagnostics.py`, `_harness_factory.py`, `_dashboard_harness.py`. `WaitOutcome.format_message()` is now the canonical timeout-error shape across the integration fixtures and `test_replay_fixture._wait_for_replay_mode`.
```

- [ ] **Step 6: Commit final cleanup**

```bash
git add tests/integration/conftest.py docs/superpowers/scans/2026-05-17-improve-2220.md
git commit -m "tests/integration: CF-1 final cleanup + scan update

Removes leftover unused imports from conftest after the extractions.
Updates the 2026-05-17-2220 scan file to mark CF-1 shipped."
```

---

## Notes for the implementer

- **Do not refactor production code in this plan.** CF-1 is strictly test-infra. If you spot a bug in `python/spinlab/` while reading, file it for a follow-up scan; don't fix it inline.
- **Frequent commits.** Each task is its own commit. Do not batch multiple tasks into one commit — the small commits give a clear bisect surface if a test regression appears later.
- **The pyright baseline drifts.** Before each task's type-check step, capture the current main-branch error count for the files you're touching, and compare. New errors block; matching count is fine.
- **`pytest_runtest_makereport` and `pytest_runtest_setup` MUST stay in conftest.py.** Pytest auto-discovers hooks from `conftest.py` files only. Moving them to `_diagnostics.py` would silently disable them.
- **`pytestmark = pytest.mark.emulator`** must also stay in conftest.py. It applies to every test collected under `tests/integration/` regardless of whether the test uses an emulator-marked fixture (the `test_frontend_smoke.py` case relies on this marker even though it uses the fake backend).
- **`LOVE_YOURSELF_GAME_ID` is imported by `test_replay_fixture.py:18`.** After Task 2 it lives in `_rom_paths.py`. The conftest's `from tests.integration._rom_paths import ... LOVE_YOURSELF_GAME_ID ...` re-exports it at the conftest module level, so the existing import in `test_replay_fixture.py` keeps working. Verify this in Task 2 Step 4. If pytest somehow complains, change the import in `test_replay_fixture.py` to point at `_rom_paths` directly.
