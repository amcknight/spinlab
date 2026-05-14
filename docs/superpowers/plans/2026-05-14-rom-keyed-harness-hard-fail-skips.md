# ROM-keyed RA harness factory + hard-fail emulator skips (C1-C3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the fragile auto-pick ROM selection in `tests/integration/conftest.py` with a session-scoped ROM-keyed harness factory, and convert every `pytest.skip` / `skipif` on missing emulator infrastructure into a hard `RuntimeError`, so a missing ROM / RA binary / launch flake produces a red test instead of a silent skip (per CLAUDE.md's standing rule and `feedback_run_emulator_tests` memory).

**Architecture:** A session-scoped `ra_harness_factory` fixture takes a `rom_key` (e.g. `"default"`, `"love_yourself"`), looks up the ROM filename in a small in-module registry, resolves the RA binary + core from `config.yaml`, and launches one `RAHarness` per unique key — cached for the rest of the session. Existing `ra_harness` and `ra_harness_love_yourself` fixtures become thin shims over the factory so their ~12 call sites keep working unchanged. Missing infrastructure (`retroarch_path`, `ra_core_path`, ROM file, NCI startup failure, WRAM sanity-probe failure) raises `RuntimeError` instead of `pytest.skip` — the genuinely-acceptable skip path is preserved only for the `pytestmark = pytest.mark.emulator` module-wide opt-out (already handled by pytest's `-m` selector, no skip call needed).

**Tech Stack:** pytest, pytest-asyncio, RetroArch 1.22.2 (vendored at `C:/RetroArch-Win64-fixed`) + snes9x_libretro, NCI over UDP, Python 3.11+.

**Context for the executing engineer:** This plan was generated from the /improve scan at `docs/superpowers/scans/2026-05-14-improve-1735.md`. Two real bugs were verified inline during scan:

1. `_test_rom_path()` at `tests/integration/conftest.py:52-66` picks the alphabetically-first `.smc` in `config.yaml`'s `rom.dir`. On the dev machine that's `'the.smc` which fails the FRAMEADVANCE WRAM-changes sanity probe (apparent deep-freeze) → all 11 `ra_harness`-dependent tests skip silently with reason `"ra_harness launch failed: core may be in deep-freeze"`.
2. The `pytest.skip(...)` calls inside the `ra_harness` and `ra_harness_love_yourself` fixtures (`conftest.py:289, 303, 329, 343`) and the module-level `skip_no_love_yourself` (`:85-88`) violate CLAUDE.md's explicit "an env var the harness needs (e.g. `SPINLAB_TEST_ROM`) — is a failure, not a skip" rule. Andrew has corrected this pattern twice (memory: `feedback_run_emulator_tests`, `feedback_red_baseline_habit`).

**Why a registry/factory and not just "use Love Yourself for both":** Andrew explicitly invoked the design intent of "we did the work to make simultaneous parallel RAs to handle a small number of different ROMs" and asked to "develop for at least two ROMs to keep the muscle." The factory therefore wires `"default"` to `Toothpaste.smc` (verified present in the dev `rom.dir` during plan-writing) and `"love_yourself"` to `Love Yourself.smc` from day one. That preserves `test_two_harnesses_use_distinct_nci_ports`, exercises two genuinely distinct ROMs in parallel on every emulator-suite run, and bounds the future shape: per Andrew the expected long-run ROM count is 2-5 total, so a tiny in-module registry is the right level of abstraction — no per-ROM config file, no dynamic discovery, just a `dict[str, str]` that grows by one line per new entrant.

---

## File Structure

**Modified:**
- `tests/integration/conftest.py` — rewrite the RA-related half of the file. Specifically:
  - Delete: `_test_rom_path()` (lines 52-66), `_love_yourself_rom_path()` (69-80), `_love_yourself_rom` module var (83), `skip_no_love_yourself` (85-88), `_ra_paths()` (249-260), `_ra_paths_love_yourself()` (263-274), the body of the `ra_harness` fixture (277-308), the body of the `ra_harness_love_yourself` fixture (311-348).
  - Add: `ROM_REGISTRY` constant, `_resolve_rom_path()` helper, `_resolve_ra_paths()` helper, `ra_harness_factory` session-scoped fixture, rewritten thin-shim `ra_harness` and `ra_harness_love_yourself` fixtures.
  - Keep unchanged: `LOVE_YOURSELF_GAME_ID` constant (imported by `test_replay_fixture.py:18`), `_free_port`, `_free_udp_port`, `_hard_kill`, `_load_config`, `fake_dashboard_server`, `fake_game_loaded`, `run_scenario`, `replay_ra_dashboard`, the diagnostic hook (`pytest_runtest_makereport`).
  - Adjust `replay_ra_dashboard` (lines 368-494): replace its lookup of `_love_yourself_rom` (line 407) with `_resolve_rom_path("love_yourself")` so it shares the same ROM-resolution path as the factory.
- `tests/integration/test_replay_fixture.py:18,20` — drop the `skip_no_love_yourself` import and remove it from the `pytestmark` list.
- `tests/integration/README.md` — update the section that describes `ra_harness` / `ra_harness_love_yourself` (line 33-34, 160) to mention the factory + registry, plus drop any "skip if missing" language.

**Created:**
- `tests/unit/integration/test_ra_harness_factory.py` — unit tests for the new `_resolve_rom_path`, `_resolve_ra_paths`, and a smoke test that the factory raises `RuntimeError` (not `pytest.skip`) on missing infrastructure. Mocks `RAHarness.launch` so no real RA is needed.

**Not touched:**
- `tests/integration/ra_harness.py` (the `RAHarness` class itself is already correct).
- Production code (this is test-infrastructure only).

---

## Implementation Tasks

### Task 1: Add ROM_REGISTRY + `_resolve_rom_path` helper with failing test

**Files:**
- Modify: `tests/integration/conftest.py` (add new module-level definitions; do NOT delete old code yet)
- Create: `tests/unit/integration/test_ra_harness_factory.py`

- [ ] **Step 1.1: Write the failing test**

Create `tests/unit/integration/test_ra_harness_factory.py`:

```python
"""Tests for ROM registry + path resolvers used by ra_harness_factory.

These tests must NOT require RetroArch installed — they exercise the
resolver/factory plumbing only. Real RA launch is mocked.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest


def test_resolve_rom_path_returns_path_when_rom_present(tmp_path):
    from tests.integration.conftest import _resolve_rom_path, ROM_REGISTRY

    rom_dir = tmp_path / "roms"
    rom_dir.mkdir()
    # The default key must exist in the registry; create a file matching its filename.
    filename = ROM_REGISTRY["default"]
    (rom_dir / filename).write_bytes(b"\x00")

    fake_config = {"rom": {"dir": str(rom_dir)}}
    with patch("tests.integration.conftest._load_config", return_value=fake_config):
        path = _resolve_rom_path("default")

    assert path == rom_dir / filename


def test_resolve_rom_path_raises_on_unknown_key():
    from tests.integration.conftest import _resolve_rom_path

    with pytest.raises(RuntimeError, match="unknown rom_key"):
        _resolve_rom_path("not_a_real_key")


def test_resolve_rom_path_raises_on_missing_rom_file(tmp_path):
    from tests.integration.conftest import _resolve_rom_path

    rom_dir = tmp_path / "roms"
    rom_dir.mkdir()
    # rom_dir exists but the registered filename isn't in it.
    fake_config = {"rom": {"dir": str(rom_dir)}}
    with patch("tests.integration.conftest._load_config", return_value=fake_config):
        with pytest.raises(RuntimeError, match="ROM file not found"):
            _resolve_rom_path("default")


def test_resolve_rom_path_raises_on_missing_rom_dir_in_config():
    from tests.integration.conftest import _resolve_rom_path

    with patch("tests.integration.conftest._load_config", return_value={}):
        with pytest.raises(RuntimeError, match="rom.dir not configured"):
            _resolve_rom_path("default")
```

- [ ] **Step 1.2: Run test to verify it fails**

Run: `python -m pytest tests/unit/integration/test_ra_harness_factory.py -v`
Expected: 4 tests, all ERROR or FAIL with `ImportError: cannot import name 'ROM_REGISTRY'` and `'_resolve_rom_path'` from `tests.integration.conftest`.

- [ ] **Step 1.3: Add ROM_REGISTRY and `_resolve_rom_path` to conftest.py**

Insert these near the top of `tests/integration/conftest.py`, after the existing `LOVE_YOURSELF_GAME_ID = "bd94dbb29012c7f5"` line. Also add a new constant for the default ROM filename:

```python
TOOTHPASTE_ROM_NAME = "Toothpaste.smc"

# ---------------------------------------------------------------------------
# ROM registry: rom_key -> filename in config.yaml's rom.dir.
#
# Each entry produces one cached session-scoped RAHarness in
# `ra_harness_factory`. Even when two keys point at the same filename, they
# produce two distinct RA processes (different cache entries) — preserves
# `test_two_harnesses_use_distinct_nci_ports` and the broader "parallel RAs"
# infrastructure (see project_pytest_xdist_experiment_2026_05_11 memory).
#
# Today:
#   - "default" -> Toothpaste.smc — used by poke transitions, harness
#     isolation, practice smoke. These tests poke generic SMW WRAM addresses
#     so any vanilla-ish SMW base works.
#   - "love_yourself" -> Love Yourself.smc — pinned ROM for the replay
#     fixture (replay was recorded against this ROM, so it must match).
#
# Expected long-run size: 2-5 entries total per Andrew. To add a test that
# needs a different ROM, add a key/filename pair here and request it via
# `ra_harness_factory("<key>")`.
ROM_REGISTRY: dict[str, str] = {
    "default": TOOTHPASTE_ROM_NAME,
    "love_yourself": LOVE_YOURSELF_ROM_NAME,
}


def _resolve_rom_path(rom_key: str) -> Path:
    """Resolve a registered rom_key to an absolute Path under config.yaml's rom.dir.

    Hard-fail on every step:
      - unknown rom_key (registry typo)
      - rom.dir missing from config.yaml
      - rom.dir set but the registered filename isn't present on disk

    Hard-fail (RuntimeError) rather than pytest.skip so missing infrastructure
    surfaces as a red test per CLAUDE.md ("an env var the harness needs - is
    a failure, not a skip").
    """
    if rom_key not in ROM_REGISTRY:
        raise RuntimeError(
            f"unknown rom_key {rom_key!r}; known keys: {sorted(ROM_REGISTRY)}"
        )
    config = _load_config()
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
```

- [ ] **Step 1.4: Run test to verify it passes**

Run: `python -m pytest tests/unit/integration/test_ra_harness_factory.py -v`
Expected: 4 passed.

- [ ] **Step 1.5: Run full fast suite to ensure nothing else broke**

Run: `python -m pytest -m "not emulator"`
Expected: All passes (no regressions); the existing `_test_rom_path` / `_love_yourself_rom_path` are still in place so old fixtures still work — they're just dead-code-adjacent now.

- [ ] **Step 1.6: Commit**

```bash
git add tests/integration/conftest.py tests/unit/integration/test_ra_harness_factory.py
git commit -m "tests: add ROM_REGISTRY + _resolve_rom_path resolver

Adds the registry side of the ROM-keyed harness factory. No call-site changes
yet. Old _test_rom_path / _love_yourself_rom_path / skip_no_love_yourself
remain in place; they'll be removed once the factory + shims are wired
(next task)."
```

---

### Task 2: Add `_resolve_ra_paths` helper with failing test

**Files:**
- Modify: `tests/integration/conftest.py`
- Modify: `tests/unit/integration/test_ra_harness_factory.py`

- [ ] **Step 2.1: Write the failing test**

Append to `tests/unit/integration/test_ra_harness_factory.py`:

```python
def test_resolve_ra_paths_returns_triple(tmp_path):
    from tests.integration.conftest import _resolve_ra_paths

    exe = tmp_path / "retroarch.exe"; exe.write_bytes(b"")
    core = tmp_path / "core.dll"; core.write_bytes(b"")
    rom_dir = tmp_path / "roms"; rom_dir.mkdir()
    from tests.integration.conftest import LOVE_YOURSELF_ROM_NAME
    (rom_dir / LOVE_YOURSELF_ROM_NAME).write_bytes(b"")

    fake_config = {
        "emulator": {"retroarch_path": str(exe), "ra_core_path": str(core)},
        "rom": {"dir": str(rom_dir)},
    }
    with patch("tests.integration.conftest._load_config", return_value=fake_config):
        retroarch_exe, ra_core_path, rom_path = _resolve_ra_paths("default")

    assert retroarch_exe == exe
    assert ra_core_path == core
    assert rom_path == rom_dir / LOVE_YOURSELF_ROM_NAME


def test_resolve_ra_paths_raises_on_missing_retroarch_path(tmp_path):
    from tests.integration.conftest import _resolve_ra_paths

    rom_dir = tmp_path / "roms"; rom_dir.mkdir()
    from tests.integration.conftest import LOVE_YOURSELF_ROM_NAME
    (rom_dir / LOVE_YOURSELF_ROM_NAME).write_bytes(b"")
    fake_config = {"emulator": {}, "rom": {"dir": str(rom_dir)}}
    with patch("tests.integration.conftest._load_config", return_value=fake_config):
        with pytest.raises(RuntimeError, match="emulator.retroarch_path not configured"):
            _resolve_ra_paths("default")


def test_resolve_ra_paths_raises_on_missing_ra_core_path(tmp_path):
    from tests.integration.conftest import _resolve_ra_paths

    exe = tmp_path / "retroarch.exe"; exe.write_bytes(b"")
    rom_dir = tmp_path / "roms"; rom_dir.mkdir()
    from tests.integration.conftest import LOVE_YOURSELF_ROM_NAME
    (rom_dir / LOVE_YOURSELF_ROM_NAME).write_bytes(b"")
    fake_config = {"emulator": {"retroarch_path": str(exe)}, "rom": {"dir": str(rom_dir)}}
    with patch("tests.integration.conftest._load_config", return_value=fake_config):
        with pytest.raises(RuntimeError, match="emulator.ra_core_path not configured"):
            _resolve_ra_paths("default")


def test_resolve_ra_paths_raises_when_retroarch_exe_missing_on_disk(tmp_path):
    from tests.integration.conftest import _resolve_ra_paths

    nonexistent_exe = tmp_path / "does_not_exist" / "retroarch.exe"
    core = tmp_path / "core.dll"; core.write_bytes(b"")
    rom_dir = tmp_path / "roms"; rom_dir.mkdir()
    from tests.integration.conftest import LOVE_YOURSELF_ROM_NAME
    (rom_dir / LOVE_YOURSELF_ROM_NAME).write_bytes(b"")
    fake_config = {
        "emulator": {"retroarch_path": str(nonexistent_exe), "ra_core_path": str(core)},
        "rom": {"dir": str(rom_dir)},
    }
    with patch("tests.integration.conftest._load_config", return_value=fake_config):
        with pytest.raises(RuntimeError, match="retroarch_path does not exist"):
            _resolve_ra_paths("default")
```

- [ ] **Step 2.2: Run test to verify it fails**

Run: `python -m pytest tests/unit/integration/test_ra_harness_factory.py -v -k _resolve_ra_paths`
Expected: 4 tests ERROR with `ImportError: cannot import name '_resolve_ra_paths'`.

- [ ] **Step 2.3: Add `_resolve_ra_paths` to conftest.py**

Insert immediately after `_resolve_rom_path` (which Task 1 added):

```python
def _resolve_ra_paths(rom_key: str) -> tuple[Path, Path, Path]:
    """Resolve (retroarch_exe, ra_core_path, rom_path) for a given rom_key.

    Hard-fail if any of:
      - emulator.retroarch_path missing/empty in config.yaml
      - emulator.ra_core_path missing/empty in config.yaml
      - retroarch_path file does not exist on disk
      - ra_core_path file does not exist on disk
      - rom_path can't be resolved by `_resolve_rom_path` (propagates)
    """
    config = _load_config()
    emu = config.get("emulator", {})
    exe_str = emu.get("retroarch_path")
    core_str = emu.get("ra_core_path")
    if not exe_str:
        raise RuntimeError(
            "emulator.retroarch_path not configured in config.yaml"
        )
    if not core_str:
        raise RuntimeError(
            "emulator.ra_core_path not configured in config.yaml"
        )
    exe = Path(exe_str)
    core = Path(core_str)
    if not exe.exists():
        raise RuntimeError(f"retroarch_path does not exist on disk: {exe}")
    if not core.exists():
        raise RuntimeError(f"ra_core_path does not exist on disk: {core}")
    rom_path = _resolve_rom_path(rom_key)
    return exe, core, rom_path
```

- [ ] **Step 2.4: Run test to verify it passes**

Run: `python -m pytest tests/unit/integration/test_ra_harness_factory.py -v`
Expected: 8 passed (4 from Task 1 + 4 new).

- [ ] **Step 2.5: Commit**

```bash
git add tests/integration/conftest.py tests/unit/integration/test_ra_harness_factory.py
git commit -m "tests: add _resolve_ra_paths helper for harness factory

Resolves (exe, core, rom_path) for a rom_key; hard-fails on missing
config keys or missing files. Pairs with _resolve_rom_path from the
prior commit."
```

---

### Task 3: Add `ra_harness_factory` session-scoped fixture with failing test

**Files:**
- Modify: `tests/integration/conftest.py`
- Modify: `tests/unit/integration/test_ra_harness_factory.py`

- [ ] **Step 3.1: Write the failing test**

Append to `tests/unit/integration/test_ra_harness_factory.py`:

```python
def test_factory_caches_per_key(tmp_path):
    """factory(key) returns same instance on subsequent calls; factory(key1) and
    factory(key2) return DIFFERENT instances even if their ROMs happen to match."""
    from tests.integration.conftest import _harness_factory_impl
    from tests.integration.ra_harness import RAHarness

    h_default = MagicMock(spec=RAHarness)
    h_love = MagicMock(spec=RAHarness)
    launched = [h_default, h_love]

    with patch(
        "tests.integration.conftest.RAHarness.launch",
        side_effect=lambda **kw: launched.pop(0),
    ), patch(
        "tests.integration.conftest._resolve_ra_paths",
        return_value=(Path("exe"), Path("core"), Path("rom")),
    ), patch(
        "tests.integration.conftest._free_udp_port",
        side_effect=[55001, 55002],
    ):
        factory_impl = _harness_factory_impl()
        a1 = factory_impl("default")
        a2 = factory_impl("default")
        b = factory_impl("love_yourself")

    assert a1 is a2  # cached
    assert a1 is not b  # distinct keys -> distinct instances


def test_factory_raises_runtime_error_on_launch_failure(tmp_path):
    """If RAHarness.launch raises RAHarnessLaunchError, factory must surface it
    as a RuntimeError (not pytest.skip)."""
    from tests.integration.conftest import _harness_factory_impl
    from tests.integration.ra_harness import RAHarnessLaunchError

    with patch(
        "tests.integration.conftest.RAHarness.launch",
        side_effect=RAHarnessLaunchError("simulated deep-freeze"),
    ), patch(
        "tests.integration.conftest._resolve_ra_paths",
        return_value=(Path("exe"), Path("core"), Path("rom")),
    ), patch(
        "tests.integration.conftest._free_udp_port",
        return_value=55001,
    ):
        factory_impl = _harness_factory_impl()
        with pytest.raises(RuntimeError, match="ra_harness launch failed.*simulated deep-freeze"):
            factory_impl("default")


def test_factory_propagates_resolver_runtime_errors(tmp_path):
    """If _resolve_ra_paths raises RuntimeError, factory must NOT swallow it."""
    from tests.integration.conftest import _harness_factory_impl

    with patch(
        "tests.integration.conftest._resolve_ra_paths",
        side_effect=RuntimeError("rom.dir not configured in config.yaml"),
    ):
        factory_impl = _harness_factory_impl()
        with pytest.raises(RuntimeError, match="rom.dir not configured"):
            factory_impl("default")


def test_factory_teardown_calls_each_harness(tmp_path):
    """Factory must teardown every cached harness exactly once when the
    fixture's `yield` returns."""
    from tests.integration.conftest import _harness_factory_impl
    from tests.integration.ra_harness import RAHarness

    h_a = MagicMock(spec=RAHarness)
    h_b = MagicMock(spec=RAHarness)
    launched = [h_a, h_b]

    with patch(
        "tests.integration.conftest.RAHarness.launch",
        side_effect=lambda **kw: launched.pop(0),
    ), patch(
        "tests.integration.conftest._resolve_ra_paths",
        return_value=(Path("exe"), Path("core"), Path("rom")),
    ), patch(
        "tests.integration.conftest._free_udp_port",
        side_effect=[55001, 55002],
    ):
        factory_impl = _harness_factory_impl()
        factory_impl("default")
        factory_impl("love_yourself")
        factory_impl.teardown_all()

    h_a.teardown.assert_called_once()
    h_b.teardown.assert_called_once()
```

Note: `_harness_factory_impl` and the `teardown_all` affordance below are deliberately exposed so unit tests can drive the cache+teardown logic without a real `pytest.fixture` lifecycle. The pytest fixture wraps this.

Also add `from unittest.mock import MagicMock` to the top of the test file if not already imported.

- [ ] **Step 3.2: Run test to verify it fails**

Run: `python -m pytest tests/unit/integration/test_ra_harness_factory.py -v -k factory`
Expected: 4 tests ERROR with `ImportError: cannot import name '_harness_factory_impl'`.

- [ ] **Step 3.3: Add `_harness_factory_impl` and `ra_harness_factory` fixture to conftest.py**

Insert after `_resolve_ra_paths` (Task 2), and somewhere above the existing `ra_harness` fixture definition (which is still present from before; we'll rewrite it in Task 4):

```python
class _HarnessFactory:
    """Session-scoped cache mapping rom_key -> RAHarness.

    Separated from the pytest fixture so unit tests can drive the cache and
    teardown logic without a real fixture lifecycle.
    """

    def __init__(self) -> None:
        self._cache: dict[str, "RAHarness"] = {}

    def __call__(self, rom_key: str) -> "RAHarness":
        from tests.integration.ra_harness import RAHarness, RAHarnessLaunchError

        if rom_key in self._cache:
            return self._cache[rom_key]
        retroarch_exe, ra_core_path, rom_path = _resolve_ra_paths(rom_key)
        try:
            harness = RAHarness.launch(
                rom_path=rom_path,
                core_path=ra_core_path,
                retroarch_exe=retroarch_exe,
                nci_port=_free_udp_port(),
            )
        except RAHarnessLaunchError as exc:
            # CLAUDE.md: launch failure is a FAILURE, not a skip.
            raise RuntimeError(f"ra_harness launch failed for rom_key={rom_key!r}: {exc}") from exc
        self._cache[rom_key] = harness
        return harness

    def teardown_all(self) -> None:
        while self._cache:
            _key, harness = self._cache.popitem()
            try:
                harness.teardown()
            except Exception:
                # Best-effort: surface in the log, don't mask the original test failure.
                import logging
                logging.getLogger(__name__).exception("ra_harness teardown failed for %r", _key)


def _harness_factory_impl() -> _HarnessFactory:
    """Factory constructor surface used by both the pytest fixture and unit tests."""
    return _HarnessFactory()


@pytest.fixture(scope="session")
def ra_harness_factory():
    """Session-scoped factory: factory(rom_key) -> RAHarness, cached per rom_key.

    Hard-fails (RuntimeError) on any missing infrastructure — no pytest.skip.
    See ROM_REGISTRY for the available rom_keys.
    """
    factory = _harness_factory_impl()
    yield factory
    factory.teardown_all()
```

Also: at the top of `conftest.py`, add this import if it's not already present (used by the type hint):

```python
from tests.integration.ra_harness import RAHarness  # noqa: TC001 — runtime use in factory
```

(The factory imports `RAHarness` and `RAHarnessLaunchError` lazily inside `__call__` to avoid pulling RA-specific modules at conftest import time, mirroring the existing pattern in the old `ra_harness` fixture body at lines 280 / 320.)

- [ ] **Step 3.4: Run test to verify it passes**

Run: `python -m pytest tests/unit/integration/test_ra_harness_factory.py -v`
Expected: 12 passed (4 from each of Tasks 1, 2, 3).

- [ ] **Step 3.5: Commit**

```bash
git add tests/integration/conftest.py tests/unit/integration/test_ra_harness_factory.py
git commit -m "tests: add ra_harness_factory session-scoped fixture

Caches one RAHarness per rom_key; converts RAHarnessLaunchError to
RuntimeError so launch failures surface as red tests instead of silent
skips. Old ra_harness / ra_harness_love_yourself still in place; next
task rewrites them as factory shims."
```

---

### Task 4: Rewrite `ra_harness` / `ra_harness_love_yourself` as factory shims, remove dead helpers

**Files:**
- Modify: `tests/integration/conftest.py`

This is the biggest single edit. Steps are split for review.

- [ ] **Step 4.1: Rewrite the `ra_harness` fixture as a thin shim**

Replace lines `277-308` (`ra_harness` fixture body, including the imports inside it) with:

```python
@pytest.fixture(scope="session")
def ra_harness(ra_harness_factory):
    """Session-scoped RAHarness for poke-driven tests. Backed by the
    factory under rom_key='default'. Hard-fails on missing infrastructure
    per CLAUDE.md."""
    return ra_harness_factory("default")
```

- [ ] **Step 4.2: Rewrite the `ra_harness_love_yourself` fixture as a thin shim**

Replace lines `311-348` (`ra_harness_love_yourself` fixture body) with:

```python
@pytest.fixture(scope="session")
def ra_harness_love_yourself(ra_harness_factory):
    """Session-scoped RAHarness pinned to Love Yourself.smc. Distinct from
    `ra_harness` (different rom_key) so the two run as separate processes —
    see test_two_harnesses_use_distinct_nci_ports."""
    return ra_harness_factory("love_yourself")
```

- [ ] **Step 4.3: Delete dead helpers**

Delete entirely:
- `_test_rom_path()` (was lines 52-66)
- `_love_yourself_rom_path()` (was lines 69-80)
- The module-level call `_love_yourself_rom = _love_yourself_rom_path()` (was line 83)
- `skip_no_love_yourself = pytest.mark.skipif(...)` (was lines 85-88)
- `_ra_paths()` (was lines 249-260)
- `_ra_paths_love_yourself()` (was lines 263-274)

- [ ] **Step 4.4: Patch `replay_ra_dashboard` to use the new resolver**

In the `replay_ra_dashboard` fixture (was around line 407 before deletions), find the line:

```python
rom_dir = Path(_love_yourself_rom).parent if _love_yourself_rom else None
```

Replace with:

```python
rom_dir = _resolve_rom_path("love_yourself").parent
```

The lookup is now eager and will `RuntimeError` if ROM isn't resolvable — exactly the behavior we want. (The old code silently set `rom_dir = None` which would have caused downstream `AppConfig` validation issues anyway.)

- [ ] **Step 4.5: Verify no orphaned imports / dead references**

Run: `python -m pytest --collect-only tests/integration -q 2>&1 | tail -20`
Expected: 12 tests collected for integration suite, no import errors. If there's an `ImportError` from `test_replay_fixture.py` (which imports `skip_no_love_yourself`), that's expected — Task 5 fixes it. For now you can temporarily skip that file with `--ignore=tests/integration/test_replay_fixture.py`.

- [ ] **Step 4.6: Commit**

```bash
git add tests/integration/conftest.py
git commit -m "tests: rewrite ra_harness fixtures as factory shims

ra_harness -> ra_harness_factory('default')
ra_harness_love_yourself -> ra_harness_factory('love_yourself')

Drops _test_rom_path / _love_yourself_rom_path / skip_no_love_yourself /
_ra_paths / _ra_paths_love_yourself. The skip-on-launch-failure path that
masked the fragile alphabetical ROM picker (closed by the registry) is
gone — launch failure now hard-fails per CLAUDE.md."
```

---

### Task 5: Drop `skip_no_love_yourself` usage from test_replay_fixture.py

**Files:**
- Modify: `tests/integration/test_replay_fixture.py:18,20`

- [ ] **Step 5.1: Update the import**

Find line 18:

```python
from tests.integration.conftest import LOVE_YOURSELF_GAME_ID, skip_no_love_yourself
```

Replace with:

```python
from tests.integration.conftest import LOVE_YOURSELF_GAME_ID
```

- [ ] **Step 5.2: Update pytestmark**

Find line 20:

```python
pytestmark = [pytest.mark.emulator, skip_no_love_yourself]
```

Replace with:

```python
pytestmark = pytest.mark.emulator
```

(The Love Yourself ROM presence guard moves into `_resolve_rom_path("love_yourself")` which hard-fails — the test fails red instead of skipping.)

- [ ] **Step 5.3: Collect-only sanity check**

Run: `python -m pytest --collect-only tests/integration -q 2>&1 | tail -10`
Expected: 12 tests collected, no import errors.

- [ ] **Step 5.4: Commit**

```bash
git add tests/integration/test_replay_fixture.py
git commit -m "tests: drop skip_no_love_yourself; rely on factory hard-fail

Missing Love Yourself ROM now produces a red test (via
_resolve_rom_path RuntimeError) instead of a silent skip — matches
CLAUDE.md's 'env var the harness needs ... is a failure, not a skip'."
```

---

### Task 6: Run the full pytest suite — must be 0 skipped

**Files:** none (verification only)

- [ ] **Step 6.1: Run full pytest**

Run: `python -m pytest`
Expected: `866 passed in <time>s` — specifically 0 skipped. If any test is skipped, surface it. If any test fails, investigate before continuing (per `feedback_red_baseline_habit`).

- [ ] **Step 6.2: If emulator tests fail with a real launch error**

That's the WHOLE POINT of this plan — the failure is now visible instead of hidden. Triage:
  - If RA crashes at launch: capture the RuntimeError chain and stderr from RA (the harness sets `stdout=DEVNULL, stderr=DEVNULL` at `ra_harness.py:136-137` — temporarily change to `subprocess.PIPE` to surface RA's stderr).
  - If FRAMEADVANCE sanity probe fails on a Love Yourself launch (it shouldn't — `test_replay_produces_segments` passes today): something changed in RA install; verify `C:/RetroArch-Win64-fixed/` is intact.
  - If FRAMEADVANCE sanity probe fails on a `Toothpaste.smc` launch: Toothpaste is not validated as known-good at plan-writing time — the architecture's whole point is to surface this failure red. Investigate: does Toothpaste boot when launched manually? If it deep-freezes like `'the.smc` did, swap `TOOTHPASTE_ROM_NAME` for another file (or temporarily set `"default" -> LOVE_YOURSELF_ROM_NAME` while a permanent replacement is selected) and surface to Andrew.
  - If `rom.dir` doesn't contain `Love Yourself.smc` or `Toothpaste.smc`: surface to Andrew; CLAUDE.md says ask before deferring.

- [ ] **Step 6.3: If something else fails that wasn't failing before**

Roll back the failing commit(s) and surface. Do not commit over a red baseline (per `feedback_red_baseline_habit` memory and CLAUDE.md "All must pass").

- [ ] **Step 6.4: Commit any necessary follow-up fixes from triage**

Only if Step 6.2 turned up environmental fixes (e.g. a config tweak), commit those here. Otherwise this step is a no-op.

---

### Task 7: Update integration README

**Files:**
- Modify: `tests/integration/README.md`

- [ ] **Step 7.1: Read the current README**

Run: `cat tests/integration/README.md | head -60`

Locate the sections that mention `ra_harness` and `ra_harness_love_yourself` (lines 33-34 and 160 per the grep done during plan-writing).

- [ ] **Step 7.2: Replace the "One RetroArch launch per pytest session" paragraph**

Find the existing description (around line 33-34). Replace with:

```markdown
**RA launch model.** One RetroArch process per unique `rom_key`, session-scoped
via `ra_harness_factory`. Today there are two registered keys: `default` maps to
`Toothpaste.smc` (used by poke transitions, harness isolation, practice smoke)
and `love_yourself` maps to `Love Yourself.smc` (pinned for the replay fixture).
Each key gets its own RA process — that's what keeps
`test_two_harnesses_use_distinct_nci_ports` honest. Expected long-run registry
size is 2-5 entries. To add a test that needs a different ROM, add an entry to
`ROM_REGISTRY` in `conftest.py` and request it via `ra_harness_factory("<key>")`.

**No silent skips.** Missing RA binary, missing core, missing ROM, or launch
failure all raise `RuntimeError` — never `pytest.skip` (per CLAUDE.md's
"an env var the harness needs ... is a failure, not a skip" rule).
```

- [ ] **Step 7.3: Replace the line-160 paragraph about session-scoped fixtures**

Find the existing "The session-scoped ra_harness and ra_harness_love_yourself fixtures..." sentence and update it to read:

```markdown
The session-scoped `ra_harness_factory` fixture (and its `ra_harness` /
`ra_harness_love_yourself` shims) caches one RAHarness per `rom_key`. Teardown
fires at session-end via the fixture's `yield`.
```

- [ ] **Step 7.4: Run docs check (if any)**

There's no doc-lint in this repo, so this step is "eyeball the rendered markdown" — open `tests/integration/README.md` and verify the two changes read coherently.

- [ ] **Step 7.5: Commit**

```bash
git add tests/integration/README.md
git commit -m "docs: update integration README for ROM-keyed harness factory"
```

---

## Self-Review

**Spec coverage:**
- ✅ ROM-keyed factory built (Task 3)
- ✅ Registry + resolver (Task 1)
- ✅ Path resolver (Task 2)
- ✅ Existing `ra_harness` / `ra_harness_love_yourself` callers preserved (Task 4: shims)
- ✅ All `pytest.skip` / `skipif` on emulator infra deleted (Task 4 + Task 5)
- ✅ `test_two_harnesses_use_distinct_nci_ports` continues to work — two distinct keys produce two cached harnesses (verified in Task 3.1's test `test_factory_caches_per_key`)
- ✅ Full pytest 0-skipped verified (Task 6)
- ✅ Docs updated (Task 7)

**Placeholder scan:** No "TBD" / "implement later" / placeholder steps. Every code block contains the actual code.

**Type consistency:** `_resolve_rom_path(rom_key) -> Path`, `_resolve_ra_paths(rom_key) -> tuple[Path, Path, Path]`, `_HarnessFactory.__call__(rom_key) -> RAHarness`, `ra_harness_factory` yields the `_HarnessFactory` instance. Consistent across all tasks.

**Backwards-compat checks:**
- `LOVE_YOURSELF_GAME_ID` still importable from `tests.integration.conftest` (used in `test_replay_fixture.py:18`). ✅ — not touched.
- `LOVE_YOURSELF_ROM_NAME` still defined (Task 1 references it in `ROM_REGISTRY`). ✅.
- `test_two_harnesses_use_distinct_nci_ports` doesn't import deleted helpers (verified: it only uses `ra_harness` and `ra_harness_love_yourself` fixtures). ✅.
