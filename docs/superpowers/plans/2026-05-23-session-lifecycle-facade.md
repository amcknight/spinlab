# Session Lifecycle Facade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Promote multi-step session-state transitions out of the routes layer into public methods on SessionManager, so routes stop mutating `session.mode` / `session.scheduler` directly and stop calling private `_clear_ref_and_idle()` / `_notify_sse()` / `_handle_attempt_invalidated()`.

**Architecture:** Routes become thin command translators. Each multi-step transition (invalidate attempt, start cold fill, reset game data) gets one public method on `SessionManager` that owns its full sequence — including the SSE notify and any mode flip. Bonus: add a `SessionManager.death_penalty_ms` property so the lone in-class reach into `self.capture.condition_registry.death_penalty_ms` collapses to a documented accessor. No behavior change. (Note: the convergence between this and the broader `condition_registry` ownership question is intentionally limited — the registry stays inside `ReferenceController`; only the existing `start_practice` reach gets a named gate. Full DI of the registry into `PracticeSession` is the separate CF-4-DI carry-over.)

**Tech Stack:** Python 3.11+, FastAPI, asyncio, pytest, pytest-asyncio. No new dependencies.

**Scope reference:** `docs/superpowers/scans/2026-05-23-improve.md` → "high-leverage → CF-1 — Session lifecycle facade".

---

### Task 1: Public `invalidate_current_attempt` method on SessionManager

**Goal:** Eliminate `session._handle_attempt_invalidated(AttemptInvalidatedEvent())` reach from `routes/practice.py:28`.

**Files:**
- Modify: `python/spinlab/session_manager.py` (add public method around the existing `_handle_attempt_invalidated`)
- Modify: `python/spinlab/routes/practice.py:25-29`
- Modify: `tests/unit/test_practice_invalidate_route.py` (already passes through the route — should keep passing; no test changes expected)
- Add: a unit test in `tests/unit/test_session_manager.py` (if that file doesn't exist, create it) for the public method.

- [ ] **Step 1: Write the failing test for the new public method**

Find or create `tests/unit/test_session_manager.py`. Add (or append) this test:

```python
"""Tests for SessionManager public coordinator methods."""
import pytest
from tests.conftest import FakeEmuBackend

from spinlab.db import Database
from spinlab.session_manager import SessionManager


@pytest.mark.asyncio
async def test_invalidate_current_attempt_dispatches_to_handler(tmp_path):
    """Public method routes through the same handler as the event dispatch table."""
    db = Database(tmp_path / "sm.db")
    db.upsert_game("g", "Game", "any%")
    emu = FakeEmuBackend(connected=False)
    sm = SessionManager(db=db, emu=emu, rom_dir=None, default_category="any%")
    sm.game_id = "g"
    # No active practice attempt — handler is a no-op, but must not raise.
    await sm.invalidate_current_attempt()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/unit/test_session_manager.py::test_invalidate_current_attempt_dispatches_to_handler -v`
Expected: FAIL with `AttributeError: 'SessionManager' object has no attribute 'invalidate_current_attempt'`.

- [ ] **Step 3: Add the public method to SessionManager**

In `python/spinlab/session_manager.py`, find the existing private `_handle_attempt_invalidated` handler (referenced in the event-handlers dict at line 111). Just above it (or near the other public coordinator methods like `stop_practice` at line 505), add:

```python
    async def invalidate_current_attempt(self) -> None:
        """Mark the current practice attempt as invalidated.

        Public entry point for the dashboard's invalidate button. Delegates
        to the same handler used by `route_event(AttemptInvalidatedEvent)`
        so the in-flight emu event path and the route path stay aligned.
        """
        await self._handle_attempt_invalidated(AttemptInvalidatedEvent())
```

(`AttemptInvalidatedEvent` is already imported at the top of the file — line 24.)

- [ ] **Step 4: Run the new test to verify it passes**

Run: `python -m pytest tests/unit/test_session_manager.py::test_invalidate_current_attempt_dispatches_to_handler -v`
Expected: PASS.

- [ ] **Step 5: Update the route to call the public method**

In `python/spinlab/routes/practice.py`, replace lines 25-29 with:

```python
@router.post("/practice/invalidate", response_model=OkResponse)
async def practice_invalidate(session: SessionManager = Depends(get_session)):
    """Mark the current practice attempt as invalidated."""
    await session.invalidate_current_attempt()
    return {"status": "ok"}
```

(Drop the `from spinlab.protocol import AttemptInvalidatedEvent` import at the top — it's no longer used in this file.)

- [ ] **Step 6: Verify the route test still passes**

Run: `python -m pytest tests/unit/test_practice_invalidate_route.py -v`
Expected: PASS.

- [ ] **Step 7: Run the fast suite**

Run: `python -m pytest -m "not emulator" -q`
Expected: all green (or same baseline as before).

- [ ] **Step 8: Commit**

```bash
git add python/spinlab/session_manager.py python/spinlab/routes/practice.py tests/unit/test_session_manager.py
git commit -m "session: add public invalidate_current_attempt; drop private reach from route"
```

---

### Task 2: Public `start_cold_fill` method on SessionManager

**Goal:** Move the cold-fill start sequence (precondition checks, call `self.cold_fill.start`, conditionally flip `self.mode`, notify SSE) out of `routes/system.py:start_cold_fill` and into `SessionManager`. Keep the route as a thin command translator.

The current route at `python/spinlab/routes/system.py:68-81`:

```python
@router.post("/cold-fill/start", response_model=OkResponse)
async def start_cold_fill(session: SessionManager = Depends(get_session)):
    if not session.game_id:
        raise HTTPException(status_code=400, detail="No game loaded")
    if session.mode != Mode.IDLE:
        raise HTTPException(status_code=409, detail=f"Cannot start cold fill: mode is {session.mode.value}")
    try:
        result = await session.cold_fill.start(session.game_id)
    except NotConnectedError:
        raise HTTPException(status_code=503, detail="Emulator not connected")
    if result.new_mode == Mode.COLD_FILL:
        session.mode = Mode.COLD_FILL
    await session._notify_sse()
    return {"status": "ok" if result.status != Status.NO_GAPS else "no_gaps"}
```

The route owns three responsibilities that should be in the coordinator: mode-precondition check, `cold_fill.start()` call, mode flip + SSE broadcast. After this task, the route only translates HTTP → SessionManager and SessionManager-result → HTTP.

**Files:**
- Modify: `python/spinlab/session_manager.py` (add `start_cold_fill` public method)
- Modify: `python/spinlab/routes/system.py:68-81`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_session_manager.py`:

```python
@pytest.mark.asyncio
async def test_start_cold_fill_flips_mode_when_new_mode_is_cold_fill(tmp_path, monkeypatch):
    """When cold_fill.start() reports new_mode=COLD_FILL, SessionManager flips mode and notifies SSE."""
    from spinlab.models import ActionResult, Mode, Status

    db = Database(tmp_path / "sm.db")
    db.upsert_game("g", "Game", "any%")
    emu = FakeEmuBackend(connected=True)
    sm = SessionManager(db=db, emu=emu, rom_dir=None, default_category="any%")
    sm.game_id = "g"

    captured: dict[str, str | None] = {"called_with_game_id": None}

    async def fake_start(game_id: str) -> ActionResult:
        captured["called_with_game_id"] = game_id
        return ActionResult(status=Status.STARTED, new_mode=Mode.COLD_FILL)

    monkeypatch.setattr(sm.cold_fill, "start", fake_start)

    result = await sm.start_cold_fill()

    assert captured["called_with_game_id"] == "g"
    assert sm.mode == Mode.COLD_FILL
    assert result.status == Status.STARTED


@pytest.mark.asyncio
async def test_start_cold_fill_no_gaps_does_not_flip_mode(tmp_path, monkeypatch):
    from spinlab.models import ActionResult, Mode, Status

    db = Database(tmp_path / "sm.db")
    db.upsert_game("g", "Game", "any%")
    emu = FakeEmuBackend(connected=True)
    sm = SessionManager(db=db, emu=emu, rom_dir=None, default_category="any%")
    sm.game_id = "g"
    sm.mode = Mode.IDLE

    async def fake_start(game_id: str) -> ActionResult:
        return ActionResult(status=Status.NO_GAPS, new_mode=None)

    monkeypatch.setattr(sm.cold_fill, "start", fake_start)
    result = await sm.start_cold_fill()
    assert sm.mode == Mode.IDLE
    assert result.status == Status.NO_GAPS
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/unit/test_session_manager.py -k start_cold_fill -v`
Expected: FAIL with `AttributeError: 'SessionManager' object has no attribute 'start_cold_fill'`.

- [ ] **Step 3: Implement `start_cold_fill` on SessionManager**

In `python/spinlab/session_manager.py`, near the other public coordinator methods (e.g., right after `stop_hyper_play` around line 565), add:

```python
    async def start_cold_fill(self) -> ActionResult:
        """Start the cold-fill capture loop for the current game.

        Routes call this directly; it owns the full transition (game-loaded
        check, current-mode check, controller dispatch, mode flip, SSE
        broadcast). The route layer only translates HTTP errors.
        """
        if self.game_id is None:
            raise NoGameLoadedError()
        if self.mode != Mode.IDLE:
            raise WrongModeError(self.mode)
        result = await self.cold_fill.start(self.game_id)
        if result.new_mode == Mode.COLD_FILL:
            self.mode = Mode.COLD_FILL
        await self._notify_sse()
        return result
```

`NoGameLoadedError` and `WrongModeError` are new error types — define them in `python/spinlab/errors.py` next to the existing `NotConnectedError`/`NotRunningError`:

```python
class NoGameLoadedError(Exception):
    """Action requires a loaded game; none is loaded."""


class WrongModeError(Exception):
    """Action is incompatible with the current Mode."""

    def __init__(self, current_mode: "Mode") -> None:
        super().__init__(f"Action incompatible with mode={current_mode.value}")
        self.current_mode = current_mode
```

(Import `Mode` at the top of `errors.py` under `TYPE_CHECKING` to avoid a circular import, or use `from typing import TYPE_CHECKING; if TYPE_CHECKING: from .models import Mode` and quote the annotation as `"Mode"`.)

In `session_manager.py`, import the new errors at the top:

```python
from .errors import (
    AlreadyRunningError,
    DraftPendingError,
    MissingSaveStatesError,
    NoGameLoadedError,
    NotConnectedError,
    NotRunningError,
    WrongModeError,
)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/unit/test_session_manager.py -k start_cold_fill -v`
Expected: PASS.

- [ ] **Step 5: Update the route**

In `python/spinlab/routes/system.py`, replace the `start_cold_fill` route (lines 68-81) with:

```python
@router.post("/cold-fill/start", response_model=OkResponse)
async def start_cold_fill(session: SessionManager = Depends(get_session)):
    from spinlab.errors import NoGameLoadedError, WrongModeError
    try:
        result = await session.start_cold_fill()
    except NoGameLoadedError:
        raise HTTPException(status_code=400, detail="No game loaded")
    except WrongModeError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot start cold fill: mode is {exc.current_mode.value}",
        )
    except NotConnectedError:
        raise HTTPException(status_code=503, detail="Emulator not connected")
    return {"status": "ok" if result.status != Status.NO_GAPS else "no_gaps"}
```

(The route file already imports `Status` and `Mode` at the top; the new error types are imported locally inside the route to keep the module-top imports tidy. You may hoist them if you prefer.)

- [ ] **Step 6: Add a smoke test for the route**

Append to `tests/unit/test_session_manager.py` (or create `tests/unit/test_cold_fill_route.py` if you prefer route-level isolation — match the existing convention; `test_practice_invalidate_route.py` uses TestClient):

```python
def test_cold_fill_route_returns_400_when_no_game(tmp_path):
    from fastapi.testclient import TestClient
    from spinlab.config import AppConfig, EmulatorConfig, NetworkConfig
    from spinlab.dashboard import create_app

    db = Database(":memory:")
    cfg = AppConfig(
        network=NetworkConfig(),
        emulator=EmulatorConfig(
            savestate_dir=tmp_path / "ra",
            spinlab_state_dir=tmp_path / "sl",
        ),
        data_dir=tmp_path,
        rom_dir=None,
    )
    app = create_app(db, config=cfg)
    with TestClient(app) as client:
        resp = client.post("/api/cold-fill/start")
        assert resp.status_code == 400
        assert "No game loaded" in resp.json()["detail"]
```

Run: `python -m pytest tests/unit/test_session_manager.py::test_cold_fill_route_returns_400_when_no_game -v`
Expected: PASS.

- [ ] **Step 7: Run the fast suite**

Run: `python -m pytest -m "not emulator" -q`
Expected: green (no regressions).

- [ ] **Step 8: Commit**

```bash
git add python/spinlab/session_manager.py python/spinlab/routes/system.py python/spinlab/errors.py tests/unit/test_session_manager.py
git commit -m "session: move cold-fill start transition out of route into SessionManager"
```

---

### Task 3: Public `reset_game_data` method on SessionManager

**Goal:** Pull the multi-step reset sequence (stop practice if running, clear reference if in REFERENCE mode, clear DB data, drop the cached scheduler, flip mode to IDLE) out of the route at `routes/system.py:84-98` and into one public method on SessionManager. The route becomes a thin wrapper.

The current route at `python/spinlab/routes/system.py:84-98`:

```python
@router.post("/reset", response_model=OkResponse)
async def reset_data(session: SessionManager = Depends(get_session), db: Database = Depends(get_db)):
    try:
        await session.stop_practice()
    except NotRunningError:
        pass
    if session.mode == Mode.REFERENCE:
        session._clear_ref_and_idle()
    gid = session.game_id
    if gid:
        logger.warning("reset: clearing all data for game=%s", gid)
        db.reset_game_data(gid)
    session.scheduler = None
    session.mode = Mode.IDLE
    return {"status": "ok"}
```

After this task, the route is two lines: call `await session.reset_game_data()` and return ok. The logger.warning move is intentional — the existing test (`test_reset_logging.py`) uses `caplog.at_level(... logger="spinlab.routes.system")`. Update the test logger name when moving the call site.

**Files:**
- Modify: `python/spinlab/session_manager.py` (add `reset_game_data` method)
- Modify: `python/spinlab/routes/system.py:84-98`
- Modify: `tests/unit/test_reset_logging.py` (update logger name from `spinlab.routes.system` to `spinlab.session_manager`)

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_session_manager.py`:

```python
@pytest.mark.asyncio
async def test_reset_game_data_clears_scheduler_and_resets_db(tmp_path):
    from spinlab.models import Mode

    db = Database(tmp_path / "sm.db")
    db.upsert_game("g", "Game", "any%")
    emu = FakeEmuBackend(connected=False)
    sm = SessionManager(db=db, emu=emu, rom_dir=None, default_category="any%")
    sm.game_id = "g"
    sm.mode = Mode.IDLE

    # Prime the scheduler cache so we can confirm it gets cleared.
    sched = sm.get_scheduler()
    assert sm.scheduler is sched

    await sm.reset_game_data()

    assert sm.scheduler is None
    assert sm.mode == Mode.IDLE


@pytest.mark.asyncio
async def test_reset_game_data_clears_reference_mode(tmp_path):
    from spinlab.models import Mode

    db = Database(tmp_path / "sm.db")
    db.upsert_game("g", "Game", "any%")
    emu = FakeEmuBackend(connected=False)
    sm = SessionManager(db=db, emu=emu, rom_dir=None, default_category="any%")
    sm.game_id = "g"
    sm.mode = Mode.REFERENCE

    await sm.reset_game_data()

    assert sm.mode == Mode.IDLE
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/unit/test_session_manager.py -k reset_game_data -v`
Expected: FAIL with `AttributeError: 'SessionManager' object has no attribute 'reset_game_data'`.

- [ ] **Step 3: Implement `reset_game_data` on SessionManager**

In `python/spinlab/session_manager.py`, add (near `switch_game` around line 182, since it touches similar invariants):

```python
    async def reset_game_data(self) -> None:
        """Stop active practice, clear reference state, drop all data for the
        current game, and return to IDLE.

        Public entry for `POST /api/reset`. Owns the full transition; the
        route is a thin wrapper. The DB wipe is gated on `game_id` so a no-op
        reset on a fresh dashboard is idempotent.
        """
        try:
            await self.stop_practice()
        except NotRunningError:
            pass
        if self.mode == Mode.REFERENCE:
            self._clear_ref_and_idle()
        gid = self.game_id
        if gid:
            logger.warning("reset: clearing all data for game=%s", gid)
            self.db.reset_game_data(gid)
        self.scheduler = None
        self.mode = Mode.IDLE
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/unit/test_session_manager.py -k reset_game_data -v`
Expected: PASS.

- [ ] **Step 5: Slim down the route**

In `python/spinlab/routes/system.py`, replace the `reset_data` route (lines 84-98) with:

```python
@router.post("/reset", response_model=OkResponse)
async def reset_data(session: SessionManager = Depends(get_session)):
    await session.reset_game_data()
    return {"status": "ok"}
```

(Drop the `db: Database = Depends(get_db)` parameter — the route no longer reaches into the DB; `SessionManager` already owns `self.db`. Also remove the now-unused `from spinlab.errors import NotConnectedError, NotRunningError` if neither is referenced elsewhere in the file — re-check after this edit.)

- [ ] **Step 6: Update the logger name in `test_reset_logging.py`**

In `tests/unit/test_reset_logging.py`, replace the two `caplog.at_level(... logger="spinlab.routes.system")` calls with `logger="spinlab.session_manager"`. Both tests still call `await reset_data(session=sm, db=db)` — change them to call `await sm.reset_game_data()` directly (more honest about what's being tested). The full file becomes:

```python
"""Test that reset_game_data emits a structured WARNING log.

Uses caplog to capture real log output regardless of which logger emits
the warning — survives refactors that move the warning into a helper
without breaking the test silently.
"""
import logging

import pytest
from tests.conftest import FakeEmuBackend

from spinlab.db import Database
from spinlab.models import Mode
from spinlab.session_manager import SessionManager


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "reset.db")
    d.upsert_game("abc123", "Test Game", "any%")
    return d


@pytest.fixture
def emu():
    return FakeEmuBackend(connected=False)


@pytest.mark.asyncio
async def test_reset_logs_warning_with_game_id(db, emu, caplog):
    """reset_game_data should emit a WARNING log containing the game id."""
    sm = SessionManager(db=db, emu=emu, rom_dir=None, default_category="any%")
    sm.game_id = "abc123"
    sm.mode = Mode.IDLE

    with caplog.at_level(logging.WARNING, logger="spinlab.session_manager"):
        await sm.reset_game_data()

    assert any(
        record.levelno == logging.WARNING and "abc123" in record.getMessage()
        for record in caplog.records
    ), f"expected WARNING mentioning 'abc123'; got {[(r.levelname, r.getMessage()) for r in caplog.records]}"


@pytest.mark.asyncio
async def test_reset_does_not_log_when_no_game_loaded(db, emu, caplog):
    """No game_id → no warning (and reset still succeeds idempotently)."""
    sm = SessionManager(db=db, emu=emu, rom_dir=None, default_category="any%")
    sm.game_id = None

    with caplog.at_level(logging.WARNING, logger="spinlab.session_manager"):
        await sm.reset_game_data()

    assert all(
        "reset: clearing all data" not in record.getMessage()
        for record in caplog.records
    )
```

- [ ] **Step 7: Run the route + reset tests**

Run: `python -m pytest tests/unit/test_reset_logging.py tests/unit/test_session_manager.py -v`
Expected: PASS.

- [ ] **Step 8: Run the fast suite**

Run: `python -m pytest -m "not emulator" -q`
Expected: green.

- [ ] **Step 9: Commit**

```bash
git add python/spinlab/session_manager.py python/spinlab/routes/system.py tests/unit/test_reset_logging.py tests/unit/test_session_manager.py
git commit -m "session: move reset transition out of route into SessionManager"
```

---

### Task 4: `death_penalty_ms` accessor (A3/A6 convergence)

**Goal:** Replace the only known caller-reach into `self.capture.condition_registry.death_penalty_ms` (at `session_manager.py:486` inside `start_practice`) with a named property on SessionManager. Routes never accessed this directly; the leak was internal to SessionManager. This is the absorbed A3/A6 work — small but worth doing in the same pass because we're already adding public API.

**Files:**
- Modify: `python/spinlab/session_manager.py` (add property; use it in `start_practice`)

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_session_manager.py`:

```python
def test_death_penalty_ms_property_forwards_to_condition_registry(tmp_path):
    db = Database(tmp_path / "sm.db")
    db.upsert_game("g", "Game", "any%")
    emu = FakeEmuBackend(connected=False)
    sm = SessionManager(db=db, emu=emu, rom_dir=None, default_category="any%")

    # Default registry value — pin to whatever ConditionRegistry exposes today
    # by reading from the registry directly, then asserting the property
    # returns the same value.
    expected = sm.capture.condition_registry.death_penalty_ms
    assert sm.death_penalty_ms == expected
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/unit/test_session_manager.py::test_death_penalty_ms_property_forwards_to_condition_registry -v`
Expected: FAIL with `AttributeError`.

- [ ] **Step 3: Add the property**

In `python/spinlab/session_manager.py`, near the other `@property` definitions (around line 144 after `current_session_id`):

```python
    @property
    def death_penalty_ms(self) -> int:
        """Time penalty (ms) added to attempts that died.

        Lives in the ConditionRegistry owned by ReferenceController; this
        accessor lets practice / hyper_play / future readers reach it without
        dotting into capture internals. Full DI of the registry is the
        separate CF-4-DI carry-over.
        """
        return self.capture.condition_registry.death_penalty_ms
```

Then in `start_practice` (line 486), replace `death_penalty_ms=self.capture.condition_registry.death_penalty_ms` with `death_penalty_ms=self.death_penalty_ms`:

```python
        ps = PracticeSession(
            emu=self.emu, db=self.db, game_id=self.require_game(),
            death_penalty_ms=self.death_penalty_ms,
            on_attempt=lambda _: asyncio.create_task(self._notify_sse()),
        )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/unit/test_session_manager.py::test_death_penalty_ms_property_forwards_to_condition_registry -v`
Expected: PASS.

- [ ] **Step 5: Run the fast suite**

Run: `python -m pytest -m "not emulator" -q`
Expected: green. (No behavior change — only a forwarding rename.)

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/session_manager.py tests/unit/test_session_manager.py
git commit -m "session: add death_penalty_ms accessor; drop reach into capture.condition_registry"
```

---

### Task 5: Verify _private symbols are now truly internal

**Goal:** After Tasks 1–3, `_handle_attempt_invalidated`, `_clear_ref_and_idle`, and `_notify_sse` should have no callers outside `python/spinlab/session_manager.py`. Run a grep to confirm, then commit either a one-line docstring update or — if anything still calls them — surface it and re-evaluate.

**Files:**
- Read-only: every file under `python/` and `tests/` (grep audit)
- Modify: `python/spinlab/session_manager.py` (docstring on each former-callable-from-outside private method, optional)

- [ ] **Step 1: Grep for cross-module callers**

Run:

```bash
git grep -nE "session\._handle_attempt_invalidated|session\._clear_ref_and_idle|session\._notify_sse" -- "python/spinlab/routes/" "tests/"
```

Expected: empty output (no matches).

Also check the broader picture:

```bash
git grep -nE "\._handle_attempt_invalidated|\._clear_ref_and_idle|\._notify_sse" -- ":!python/spinlab/session_manager.py" ":!docs/" ":!.claude/"
```

Expected: only matches inside `tests/` should be in `test_session_manager.py` (the new tests don't need to call them — they call the public methods). Anything else, treat as a follow-up.

- [ ] **Step 2: Add a one-line docstring (optional, nice-to-have)**

If the grep is empty, add a docstring on `_clear_ref_and_idle` clarifying it's internal:

```python
    def _clear_ref_and_idle(self) -> None:
        """Internal helper: clear reference state and return to IDLE.

        Called by `switch_game`, `start_practice`, `start_hyper_play`, and
        `reset_game_data`. External callers go through one of those public
        methods.
        """
        self.capture.clear_and_idle()
        self.mode = Mode.IDLE
```

(Skip the docstring tweak for `_notify_sse` — its existing docstring at line 204-207 already explains the contract.)

- [ ] **Step 3: Run the full fast suite once more**

Run: `python -m pytest -m "not emulator" -q`
Expected: green.

- [ ] **Step 4: Run the full suite (as required before merge per CLAUDE.md)**

Run: `python -m pytest -q`
Expected: green (or unchanged from the pre-CF-1 baseline; pre-existing red tests are not acceptable per `feedback_fix_preexisting_failures` — surface them before merging).

- [ ] **Step 5: Commit (only if Step 2 made a change)**

```bash
git add python/spinlab/session_manager.py
git commit -m "session: document _clear_ref_and_idle as internal post-facade"
```

---

## Self-Review

**1. Spec coverage:**
- ✅ `routes/practice.py:28` private reach — Task 1.
- ✅ `routes/system.py:75-81` cold-fill multi-step — Task 2.
- ✅ `routes/system.py:84-98` reset multi-step — Task 3 (also covers `session.scheduler = None` clearing).
- ✅ A3/A6 condition_registry reach in `start_practice` — Task 4.
- ✅ Verify _private symbols are unreachable from routes/tests — Task 5.
- ✅ "No behavior change" constraint: each public method preserves the exact sequence and error semantics of the original route code. Tests verify both behavior and log identity (the reset-logging test moves its logger name to match the new emit location).

**2. Placeholder scan:**
- ✅ Every code step includes the actual code (not "implement here").
- ✅ Every test step includes the actual test body.
- ✅ Every commit step includes the actual `git add` + `git commit` commands.
- ✅ Error types `NoGameLoadedError` / `WrongModeError` are defined in the same task that first uses them (Task 2).

**3. Type consistency:**
- ✅ `invalidate_current_attempt` returns `None` and is `async` — matches `_handle_attempt_invalidated`.
- ✅ `start_cold_fill` returns `ActionResult` — matches `self.cold_fill.start` and the existing `start_practice` shape.
- ✅ `reset_game_data` returns `None` — matches the route's old `{"status": "ok"}` (the route still returns that dict).
- ✅ `death_penalty_ms` returns `int` — matches `ConditionRegistry.death_penalty_ms`.

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-05-23-session-lifecycle-facade.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

**Which approach?**
