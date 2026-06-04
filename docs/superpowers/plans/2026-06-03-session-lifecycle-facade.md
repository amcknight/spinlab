# Session Lifecycle Facade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Supersedes:** `docs/superpowers/plans/2026-05-23-session-lifecycle-facade.md` (frozen per `feedback_superpowers_docs_frozen`). The 2026-05-23 plan invented `WrongModeError(Exception)` and `NoGameLoadedError(Exception)` from scratch; both are obsolete given the V5 errors layer that shipped 2026-05-27 (`NoGameLoadedError(ActionError, 409)` already in `python/spinlab/errors.py`; `dashboard.py:177-179` registers the `ActionError → JSONResponse` boundary handler). This plan reuses the V5 pattern and adds three new `ActionError` subclasses where needed.

**Goal:** Eliminate the four remaining route-layer reaches into SessionManager internals by promoting each multi-step transition to a documented public coordinator method. Harden `start_practice`'s snapshot lifecycle (try/except + logging + real-baseline test). Inject `Scheduler` into `PracticeSession` to collapse the split-brain. Document the ConditionRegistry install invariant.

**Architecture:** Routes become thin command translators. Each multi-step transition is one public async method on `SessionManager` that owns the full sequence (precondition checks → controller dispatch → mode flip → SSE broadcast → typed `ActionError` on failure). The dashboard's existing `ActionError → JSONResponse` boundary handler does the HTTP translation. No new error hierarchies; no parallel exception types.

**Tech Stack:** Python 3.11+, FastAPI, asyncio, pytest, pytest-asyncio. No new dependencies.

**Scope reference:** `docs/superpowers/scans/2026-06-03-improve.md` → CF1 absorbing M1, M2, M3, M5.

---

## Anchor questions (resolved before tasks)

**Q1: Does the existing `ActionError` hierarchy already cover the wrong-mode case?**
**A:** No. `errors.py` has `PracticeActiveError`, `ReferenceActiveError`, `NotInReferenceError`, `AlreadyRunningError`, `NotRunningError` — all specific to particular modes-to-states. None of these is the general "the current mode is incompatible with this action" form needed for `start_cold_fill` (which requires `mode == IDLE`) or `reset_data` (which has mixed semantics). **Add a new `WrongModeError(ActionError)` with `http_code=409`, `detail="wrong_mode"`, and a `current_mode: Mode` attribute the route can read for a human-readable response message.** This is one new error type, following the V5 pattern.

**Q2: Should `start_practice` snapshot rollback bubble an `ActionError(409)` or a new error class?**
**A:** Snapshot failure is an internal-state-construction failure, not a user-action conflict. It deserves its own typed error so the route can map it cleanly. **Add `SnapshotFailedError(ActionError)` with `http_code=500`, `detail="snapshot_failed"`.** The catch in `start_practice` (Task D) will: (a) cancel the practice task it just created, (b) reset `mode = IDLE`, (c) clear `practice_session`/`practice_task` to None, (d) raise `SnapshotFailedError`. The user sees a 500 with `{"detail": "snapshot_failed"}` and SessionManager is in a clean IDLE state — no wedge.

**Q3: For Task F, is the ROM-load → `install_condition_registry` path already atomic enough that we only need to document, vs refactor?**
**A:** Yes, documentation-only suffices for this scope. Read confirms `session_manager.py:269-270` runs `await self.switch_game(...)` immediately followed by `await self.install_condition_registry(checksum)` inside `_handle_rom_info`, both under the same `route_event` await — no other code path can interleave (event handlers run serially via `route_event`). The "mutable-after-construction" pattern is real but the ordering is enforced by the single event handler. **Task F adds a module-level docstring section to `capture/reference.py` documenting the invariant + an `assert self.condition_registry.definitions or not self.is_recording` runtime check at the top of `set_condition_registry` to surface a violation if the invariant ever breaks.** Full DI of the registry into `PracticeSession`/`SegmentRecorder` constructors stays the separate CF-4-DI carry-over.

**Q4: Should the `routes/system.py:106-120` reset_data route's mid-body `session._clear_ref_and_idle()` reach (line 113) be absorbed into a single `session.reset_data()` method, or kept as a route-level orchestration of two public methods (`stop_practice` + new `reset_game_data`)?**
**A:** Single facade method. The route currently does: stop_practice → if mode==REFERENCE clear-and-idle → db.reset_game_data → scheduler=None + mode=IDLE. Three of those four are internal session state. **Task C adds `session.reset_data()` that owns the full sequence, including the `db.reset_game_data(game_id)` call** (the controller has access to `self.db` already). The route shrinks to `await session.reset_data(); return {"status": "ok"}`. Cleanest demarcation.

---

## File structure

**Modify:**
- `python/spinlab/errors.py` — add `WrongModeError`, `SnapshotFailedError` (Tasks B, D)
- `python/spinlab/session_manager.py` — add 4 public methods: `invalidate_current_attempt` (Task A), `start_cold_fill` (Task B), `reset_data` (Task C). Modify `start_practice` (Task D), `_take_session_snapshot` (Task D). Update `PracticeSession(...)` construction site to pass scheduler (Task E).
- `python/spinlab/practice.py` — modify `PracticeSession.__init__` to accept scheduler (Task E).
- `python/spinlab/capture/reference.py` — add invariant assertion + docstring on `set_condition_registry` (Task F).
- `python/spinlab/routes/practice.py` — replace `_handle_attempt_invalidated` reach with `invalidate_current_attempt()` call (Task A). Drop the `AttemptInvalidatedEvent` import (no longer used in this file).
- `python/spinlab/routes/system.py` — replace `start_cold_fill` body (Task B). Replace `reset_data` body (Task C).
- `tests/unit/test_session_manager.py` — add tests for each new public method (Tasks A, B, C, D).
- `tests/unit/test_session_manager_snapshot.py` — add a real-baseline test that exercises closed-form computation through `_take_session_snapshot` (Task D).
- `tests/unit/test_practice.py` (find or create) — add a Scheduler-injection test (Task E).
- `tests/unit/test_session_manager_conditions.py` — add a test for the new invariant assertion (Task F).

**Files NOT modified (in scope for context but stable):**
- `python/spinlab/dashboard.py` — boundary handler already correct.
- `python/spinlab/capture/recorder.py` — `set_condition_registry` stays as the inner setter ReferenceController calls.

---

## Task A: Public `invalidate_current_attempt()` on SessionManager

**Goal:** Eliminate `session._handle_attempt_invalidated(AttemptInvalidatedEvent())` reach from `routes/practice.py:28`.

**Files:**
- Modify: `python/spinlab/session_manager.py` (add public method just above the private `_handle_attempt_invalidated` handler around line 530, before `start_practice`)
- Modify: `python/spinlab/routes/practice.py:25-29`
- Modify: `tests/unit/test_session_manager.py`

- [ ] **Step 1: Read current state of the touchpoints**

Read `tests/unit/test_session_manager.py` to see the existing pattern for how `SessionManager` is constructed in this file (look for fixture usage / `FakeEmuBackend` import / `SessionManager(...)` call shape). Match that style in Step 2.

Run: `python -m pytest tests/unit/test_session_manager.py -v --collect-only` to list existing tests in the file so you don't shadow names.

- [ ] **Step 2: Write the failing test**

Append to `tests/unit/test_session_manager.py`:

```python
@pytest.mark.asyncio
async def test_invalidate_current_attempt_dispatches_to_handler(tmp_path):
    """Public method routes through the same handler as the event dispatch table."""
    from tests.conftest import FakeEmuBackend

    from spinlab.db import Database
    from spinlab.session_manager import SessionManager

    db = Database(tmp_path / "sm.db")
    db.upsert_game("g", "Game", "any%")
    emu = FakeEmuBackend(connected=False)
    sm = SessionManager(db=db, emu=emu, rom_dir=None, default_category="any%")
    sm.game_id = "g"
    # No active practice attempt — handler is a no-op, but must not raise.
    await sm.invalidate_current_attempt()
```

If `import pytest` is not already at the top of the file, add it. If `pytest_asyncio` mode isn't configured, check `pyproject.toml` for `asyncio_mode = "auto"` — if it's auto, drop the `@pytest.mark.asyncio` decorator; if it's strict, keep it.

- [ ] **Step 3: Run the test to verify it fails**

Run: `python -m pytest tests/unit/test_session_manager.py::test_invalidate_current_attempt_dispatches_to_handler -v`
Expected: FAIL with `AttributeError: 'SessionManager' object has no attribute 'invalidate_current_attempt'`.

- [ ] **Step 4: Add the public method to SessionManager**

Open `python/spinlab/session_manager.py`. Find the existing private `_handle_attempt_invalidated` handler (referenced in the event dispatch table at line 120). Just above the `_snapshot_inputs` method (around line 495 — locate it relative to `_snapshot_inputs` since line numbers may have drifted), or wherever the file's other public coordinator methods cluster (e.g., near `stop_practice` at line 564), add:

```python
    async def invalidate_current_attempt(self) -> None:
        """Mark the current practice attempt as invalidated.

        Public entry point for the dashboard's invalidate button. Delegates
        to the same handler used by route_event(AttemptInvalidatedEvent) so
        the in-flight emu event path and the route path stay aligned.
        """
        await self._handle_attempt_invalidated(AttemptInvalidatedEvent())
```

`AttemptInvalidatedEvent` is already imported at the top of the file (line 25 — confirm before adding a duplicate import).

- [ ] **Step 5: Run the test to verify it passes**

Run: `python -m pytest tests/unit/test_session_manager.py::test_invalidate_current_attempt_dispatches_to_handler -v`
Expected: PASS.

- [ ] **Step 6: Update the route to call the public method**

In `python/spinlab/routes/practice.py`, replace the entire file with:

```python
"""Practice start/stop/invalidate routes."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from spinlab.api_schemas import ActionResponse, OkResponse
from spinlab.session_manager import SessionManager

from ._deps import get_session

router = APIRouter(prefix="/api")


@router.post("/practice/start", response_model=ActionResponse)
async def practice_start(session: SessionManager = Depends(get_session)):
    return (await session.start_practice()).to_response()


@router.post("/practice/stop", response_model=ActionResponse)
async def practice_stop(session: SessionManager = Depends(get_session)):
    return (await session.stop_practice()).to_response()


@router.post("/practice/invalidate", response_model=OkResponse)
async def practice_invalidate(session: SessionManager = Depends(get_session)):
    """Mark the current practice attempt as invalidated."""
    await session.invalidate_current_attempt()
    return {"status": "ok"}
```

The change: `from spinlab.protocol import AttemptInvalidatedEvent` is dropped (the route no longer constructs the event), and `_handle_attempt_invalidated(AttemptInvalidatedEvent())` becomes `invalidate_current_attempt()`.

- [ ] **Step 7: Run the route test to verify it still passes**

Run: `python -m pytest tests/unit/test_practice_invalidate_route.py -v`
Expected: PASS.

If that test file does not exist, skip this step (no regression to verify) and proceed to Step 8.

- [ ] **Step 8: Run the fast suite**

Run: `python -m pytest -m "not emulator" -q`
Expected: all green. Baseline reference: 1133 passed before this task started (per commit 5a17b45 verification).

- [ ] **Step 9: Run pyright + ruff on changed files**

Run: `npx pyright python/spinlab/session_manager.py python/spinlab/routes/practice.py tests/unit/test_session_manager.py`
Expected: no NEW errors vs main. (261 pre-existing errors elsewhere are tracked separately.)

Run: `ruff check python/spinlab/session_manager.py python/spinlab/routes/practice.py tests/unit/test_session_manager.py`
Expected: `All checks passed!`

- [ ] **Step 10: Commit**

```bash
git add python/spinlab/session_manager.py python/spinlab/routes/practice.py tests/unit/test_session_manager.py
git commit -m "$(cat <<'EOF'
refactor(session-manager): public invalidate_current_attempt() — drop private reach from route

routes/practice.py was calling session._handle_attempt_invalidated(AttemptInvalidatedEvent())
directly, mirroring the event-dispatch table entry. Promote to a documented public
method on SessionManager so the route is a thin command translator.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task B: `WrongModeError` + public `start_cold_fill()` on SessionManager

**Goal:** Replace `routes/system.py:68-87` body's mid-route mode-precondition checks + direct `session.mode = Mode.COLD_FILL` mutation (line 85) with a single `await session.start_cold_fill()` call. Introduce `WrongModeError` as the typed expression of "current mode incompatible with this action."

**Files:**
- Modify: `python/spinlab/errors.py` (add `WrongModeError`)
- Modify: `python/spinlab/session_manager.py` (add `start_cold_fill` public method)
- Modify: `python/spinlab/routes/system.py:68-87`
- Modify: `tests/unit/test_session_manager.py`

- [ ] **Step 1: Read current state**

Read `python/spinlab/errors.py` end-to-end to see the V5 `ActionError` pattern. Read `python/spinlab/routes/system.py:68-87` to confirm the current cold-fill route shape (precondition checks, controller dispatch, mode flip, SSE notify).

- [ ] **Step 2: Add `WrongModeError` to errors.py**

Append to `python/spinlab/errors.py` (after `NotConnectedError` and the other 409s, keep grouped by HTTP code):

```python
class WrongModeError(ActionError):
    """Action is incompatible with the current Mode.

    Use when an action requires a specific mode (e.g., cold-fill start requires
    IDLE, stop_replay requires REPLAY) and the system is in something else.
    Sets `current_mode` so the route can surface a human-readable reason.
    """
    http_code = 409
    detail = "wrong_mode"

    def __init__(self, current_mode: "Mode") -> None:
        super().__init__()
        self.current_mode = current_mode
```

Add the TYPE_CHECKING import at the top of `errors.py` (after the `from __future__ import annotations` line):

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import Mode
```

The annotation `"Mode"` stays quoted to avoid a runtime import cycle.

- [ ] **Step 3: Write the failing test for `start_cold_fill`**

Append to `tests/unit/test_session_manager.py`:

```python
@pytest.mark.asyncio
async def test_start_cold_fill_flips_mode_when_new_mode_is_cold_fill(tmp_path, monkeypatch):
    """When cold_fill.start() reports new_mode=COLD_FILL, SessionManager flips mode."""
    from tests.conftest import FakeEmuBackend

    from spinlab.db import Database
    from spinlab.models import ActionResult, Mode, Status
    from spinlab.session_manager import SessionManager

    db = Database(tmp_path / "sm.db")
    db.upsert_game("g", "Game", "any%")
    emu = FakeEmuBackend(connected=True)
    sm = SessionManager(db=db, emu=emu, rom_dir=None, default_category="any%")
    sm.game_id = "g"
    sm.mode = Mode.IDLE

    captured: dict = {}

    async def fake_start(game_id: str, run_id: str) -> ActionResult:
        captured["game_id"] = game_id
        captured["run_id"] = run_id
        return ActionResult(status=Status.STARTED, new_mode=Mode.COLD_FILL)

    monkeypatch.setattr(sm.cold_fill, "start", fake_start)
    # The facade needs a run_id; stub the DB lookup.
    monkeypatch.setattr(sm.db, "get_active_capture_run", lambda gid: "run-123")

    result = await sm.start_cold_fill()

    assert captured == {"game_id": "g", "run_id": "run-123"}
    assert sm.mode == Mode.COLD_FILL
    assert result.status == Status.STARTED


@pytest.mark.asyncio
async def test_start_cold_fill_no_gaps_does_not_flip_mode(tmp_path, monkeypatch):
    """When cold_fill.start() reports new_mode=None (NO_GAPS), mode stays IDLE."""
    from tests.conftest import FakeEmuBackend

    from spinlab.db import Database
    from spinlab.models import ActionResult, Mode, Status
    from spinlab.session_manager import SessionManager

    db = Database(tmp_path / "sm.db")
    db.upsert_game("g", "Game", "any%")
    emu = FakeEmuBackend(connected=True)
    sm = SessionManager(db=db, emu=emu, rom_dir=None, default_category="any%")
    sm.game_id = "g"
    sm.mode = Mode.IDLE

    async def fake_start(game_id: str, run_id: str) -> ActionResult:
        return ActionResult(status=Status.NO_GAPS, new_mode=None)

    monkeypatch.setattr(sm.cold_fill, "start", fake_start)
    monkeypatch.setattr(sm.db, "get_active_capture_run", lambda gid: "run-123")

    result = await sm.start_cold_fill()

    assert sm.mode == Mode.IDLE
    assert result.status == Status.NO_GAPS


@pytest.mark.asyncio
async def test_start_cold_fill_raises_wrong_mode_when_not_idle(tmp_path):
    """If the session is mid-PRACTICE, start_cold_fill raises WrongModeError(current_mode=PRACTICE)."""
    from tests.conftest import FakeEmuBackend

    from spinlab.db import Database
    from spinlab.errors import WrongModeError
    from spinlab.models import Mode
    from spinlab.session_manager import SessionManager

    db = Database(tmp_path / "sm.db")
    db.upsert_game("g", "Game", "any%")
    emu = FakeEmuBackend(connected=True)
    sm = SessionManager(db=db, emu=emu, rom_dir=None, default_category="any%")
    sm.game_id = "g"
    sm.mode = Mode.PRACTICE

    with pytest.raises(WrongModeError) as ei:
        await sm.start_cold_fill()
    assert ei.value.current_mode == Mode.PRACTICE


@pytest.mark.asyncio
async def test_start_cold_fill_raises_no_game_when_no_game(tmp_path):
    """Without a loaded game, raise NoGameLoadedError (HTTP 409)."""
    from tests.conftest import FakeEmuBackend

    from spinlab.db import Database
    from spinlab.errors import NoGameLoadedError
    from spinlab.session_manager import SessionManager

    db = Database(tmp_path / "sm.db")
    emu = FakeEmuBackend(connected=True)
    sm = SessionManager(db=db, emu=emu, rom_dir=None, default_category="any%")
    # sm.game_id is None by default
    with pytest.raises(NoGameLoadedError):
        await sm.start_cold_fill()
```

- [ ] **Step 4: Run the new tests to verify they fail**

Run: `python -m pytest tests/unit/test_session_manager.py -k start_cold_fill -v`
Expected: FAIL with `AttributeError: 'SessionManager' object has no attribute 'start_cold_fill'`.

- [ ] **Step 5: Implement `start_cold_fill` on SessionManager**

In `python/spinlab/session_manager.py`, add the import for `WrongModeError` to the existing `from .errors import (...)` block (alphabetical insert between `NotRunningError` and the closing paren):

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

Near the other public coordinator methods (e.g., right after `start_practice` around line 552 or right before `_handle_*` group), add:

```python
    async def start_cold_fill(self) -> ActionResult:
        """Start the cold-fill capture loop for the current game.

        Routes call this directly; it owns the full transition (game-loaded
        check, current-mode check, active-run lookup, controller dispatch,
        mode flip, SSE broadcast). The route layer only translates
        ActionError → HTTPException via the boundary handler.
        """
        self.require_game()  # raises NoGameLoadedError if no game
        if self.mode != Mode.IDLE:
            raise WrongModeError(self.mode)
        run_id = self.db.get_active_capture_run(self.game_id)
        if run_id is None:
            # No active reference run; cold fill has nothing to fill. The
            # router maps this same condition to a 400 today; we surface it
            # as NoGameLoadedError-adjacent — but there's no NoActiveRunError
            # in the V5 hierarchy yet. Until one lands, raise WrongModeError
            # with a clear detail at the route boundary.
            #
            # NOTE: this branch could justify its own ActionError subclass
            # (NoActiveRunError(404)) in a follow-up; for now the route
            # surfaces "No active reference run" via the catch below.
            raise WrongModeError(self.mode)  # mode=IDLE, but no run to fill
        result = await self.cold_fill.start(self.game_id, run_id=run_id)
        if result.new_mode == Mode.COLD_FILL:
            self.mode = Mode.COLD_FILL
        await self._notify_sse()
        return result
```

(`require_game()` already exists at line 184; it raises `NoGameLoadedError` and returns `str`. Reuse it.)

- [ ] **Step 6: Run the new tests to verify they pass**

Run: `python -m pytest tests/unit/test_session_manager.py -k start_cold_fill -v`
Expected: PASS (all four).

- [ ] **Step 7: Update the route**

In `python/spinlab/routes/system.py`, replace the `start_cold_fill` route body (lines 68-87) with:

```python
@router.post("/cold-fill/start", response_model=OkResponse)
async def start_cold_fill(session: SessionManager = Depends(get_session)):
    """Start the cold-fill capture loop. SessionManager owns the transition;
    the dashboard's ActionError handler maps WrongModeError/NoGameLoadedError
    to 409 and NotConnectedError to 503."""
    result = await session.start_cold_fill()
    return {"status": "ok" if result.status != Status.NO_GAPS else "no_gaps"}
```

The `Mode` import at the top can be dropped from this route (no longer used inline here — verify with grep before removing). `Status` stays (used in the return). `NotConnectedError` stays (other routes use it via the existing `NotRunningError`/`NotConnectedError` import).

- [ ] **Step 8: Verify route + fast suite green**

Run: `python -m pytest -m "not emulator" -q`
Expected: all green.

- [ ] **Step 9: Run pyright + ruff on changed files**

Run: `npx pyright python/spinlab/errors.py python/spinlab/session_manager.py python/spinlab/routes/system.py tests/unit/test_session_manager.py`
Expected: no new errors.

Run: `ruff check python/spinlab/errors.py python/spinlab/session_manager.py python/spinlab/routes/system.py tests/unit/test_session_manager.py`
Expected: `All checks passed!`

- [ ] **Step 10: Commit**

```bash
git add python/spinlab/errors.py python/spinlab/session_manager.py python/spinlab/routes/system.py tests/unit/test_session_manager.py
git commit -m "$(cat <<'EOF'
refactor(session-manager): public start_cold_fill() facade + WrongModeError

routes/system.py:start_cold_fill was doing precondition checks, controller
dispatch, mode mutation, and SSE notify itself. Move the full sequence into
SessionManager.start_cold_fill(); route becomes one-liner. Add
WrongModeError(ActionError, 409) so the same boundary handler that maps
NoGameLoadedError → 409 also covers mode-conflict cases.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task C: Public `reset_data()` on SessionManager

**Goal:** Replace `routes/system.py:106-120` body's reach into `session._clear_ref_and_idle()` (line 113) + direct `session.scheduler = None` / `session.mode = Mode.IDLE` mutations (lines 118-119) with a single `await session.reset_data()` call.

**Files:**
- Modify: `python/spinlab/session_manager.py` (add `reset_data` public method)
- Modify: `python/spinlab/routes/system.py:106-120`
- Modify: `tests/unit/test_session_manager.py`

- [ ] **Step 1: Re-read the current route to understand the exact sequence**

Read `python/spinlab/routes/system.py:106-120` so the new facade method preserves the exact behavior:
- stop_practice (swallow NotRunningError) — line 109-111
- if mode==REFERENCE then `_clear_ref_and_idle()` — line 112-113
- if game_id: `logger.warning("reset: clearing all data for game=%s", gid)` + `db.reset_game_data(gid)` — line 114-117
- `session.scheduler = None` — line 118
- `session.mode = Mode.IDLE` — line 119

- [ ] **Step 2: Write the failing test**

Append to `tests/unit/test_session_manager.py`:

```python
@pytest.mark.asyncio
async def test_reset_data_clears_scheduler_and_mode(tmp_path, monkeypatch):
    """reset_data: stop practice (if any), clear ref state (if REFERENCE),
    DB-reset, clear scheduler, return to IDLE."""
    from tests.conftest import FakeEmuBackend

    from spinlab.db import Database
    from spinlab.models import Mode
    from spinlab.session_manager import SessionManager

    db = Database(tmp_path / "sm.db")
    db.upsert_game("g", "Game", "any%")
    emu = FakeEmuBackend(connected=True)
    sm = SessionManager(db=db, emu=emu, rom_dir=None, default_category="any%")
    sm.game_id = "g"
    # Force scheduler to be set so we can verify it's cleared.
    sm.scheduler = object()  # any truthy sentinel
    sm.mode = Mode.IDLE

    db_reset_calls: list[str] = []
    monkeypatch.setattr(sm.db, "reset_game_data", lambda gid: db_reset_calls.append(gid))

    await sm.reset_data()

    assert sm.scheduler is None
    assert sm.mode == Mode.IDLE
    assert db_reset_calls == ["g"]


@pytest.mark.asyncio
async def test_reset_data_clears_reference_mode_first(tmp_path, monkeypatch):
    """If currently in REFERENCE mode, reset_data must clear-and-idle the
    capture controller before DB-reset to avoid a half-finalized run."""
    from tests.conftest import FakeEmuBackend

    from spinlab.db import Database
    from spinlab.models import Mode
    from spinlab.session_manager import SessionManager

    db = Database(tmp_path / "sm.db")
    db.upsert_game("g", "Game", "any%")
    emu = FakeEmuBackend(connected=True)
    sm = SessionManager(db=db, emu=emu, rom_dir=None, default_category="any%")
    sm.game_id = "g"
    sm.mode = Mode.REFERENCE

    cleared = {"called": False}
    original_clear = sm.capture.clear_and_idle

    def spy_clear():
        cleared["called"] = True
        return original_clear()

    monkeypatch.setattr(sm.capture, "clear_and_idle", spy_clear)
    monkeypatch.setattr(sm.db, "reset_game_data", lambda gid: None)

    await sm.reset_data()

    assert cleared["called"] is True
    assert sm.mode == Mode.IDLE


@pytest.mark.asyncio
async def test_reset_data_no_game_is_noop_for_db(tmp_path, monkeypatch):
    """Without a loaded game, reset still clears scheduler+mode but skips DB."""
    from tests.conftest import FakeEmuBackend

    from spinlab.db import Database
    from spinlab.models import Mode
    from spinlab.session_manager import SessionManager

    db = Database(tmp_path / "sm.db")
    emu = FakeEmuBackend(connected=True)
    sm = SessionManager(db=db, emu=emu, rom_dir=None, default_category="any%")
    # game_id stays None
    sm.scheduler = object()
    sm.mode = Mode.IDLE

    called: list[str] = []
    monkeypatch.setattr(sm.db, "reset_game_data", lambda gid: called.append(gid))

    await sm.reset_data()

    assert sm.scheduler is None
    assert sm.mode == Mode.IDLE
    assert called == []
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `python -m pytest tests/unit/test_session_manager.py -k reset_data -v`
Expected: FAIL — `AttributeError: 'SessionManager' object has no attribute 'reset_data'`.

- [ ] **Step 4: Implement `reset_data` on SessionManager**

In `python/spinlab/session_manager.py`, add (after `start_cold_fill` from Task B):

```python
    async def reset_data(self) -> None:
        """Reset all practice/reference data for the current game.

        Full sequence: stop practice (if running), clear reference state
        (if in REFERENCE mode), nuke the per-game DB rows, clear the
        cached scheduler, return to IDLE. Replaces the per-route mutation
        sequence that routes/system.py:reset_data used to drive directly.

        NOTE: this does not broadcast SSE — the caller (typically the
        /reset route) returns immediately and the user-driven action is
        complete. State pushes happen on the next event.
        """
        from .errors import NotRunningError

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

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/unit/test_session_manager.py -k reset_data -v`
Expected: PASS (all three).

- [ ] **Step 6: Update the route**

In `python/spinlab/routes/system.py`, replace the `reset_data` route body (lines 106-120) with:

```python
@router.post("/reset", response_model=OkResponse)
async def reset_data(session: SessionManager = Depends(get_session)):
    """Reset all data for the current game. SessionManager owns the sequence."""
    await session.reset_data()
    return {"status": "ok"}
```

The `db: Database = Depends(get_db)` dependency on the route can be dropped (the facade uses `self.db`). Grep the file for any remaining direct `Mode.` references to decide if the `Mode` import can be removed; if other routes still use it, keep the import.

- [ ] **Step 7: Run the fast suite**

Run: `python -m pytest -m "not emulator" -q`
Expected: all green.

- [ ] **Step 8: Run pyright + ruff on changed files**

Run: `npx pyright python/spinlab/session_manager.py python/spinlab/routes/system.py tests/unit/test_session_manager.py`
Expected: no new errors.

Run: `ruff check python/spinlab/session_manager.py python/spinlab/routes/system.py tests/unit/test_session_manager.py`
Expected: `All checks passed!`

- [ ] **Step 9: Commit**

```bash
git add python/spinlab/session_manager.py python/spinlab/routes/system.py tests/unit/test_session_manager.py
git commit -m "$(cat <<'EOF'
refactor(session-manager): public reset_data() facade — drop route reach into private

routes/system.py:reset_data was calling session._clear_ref_and_idle() and
mutating session.scheduler/session.mode directly. Move the full sequence
(stop practice, clear ref, db-reset, scheduler=None, mode=IDLE) into
SessionManager.reset_data(); route becomes a one-liner.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task D: Harden `start_practice` snapshot lifecycle (M1 cluster)

**Goal:** `_take_session_snapshot()` currently has zero logging and is called unprotected in `start_practice`/`start_hyper_play` after the mode flip. If it raises: route returns 500, but the practice task is already running and mode is `PRACTICE`. Add structured logging, wrap the snapshot in try/except with rollback (cancel task, clear mode, clear practice_session), surface `SnapshotFailedError`. Add a real-baseline test that exercises closed-form computation through the snapshot path.

**Files:**
- Modify: `python/spinlab/errors.py` (add `SnapshotFailedError`)
- Modify: `python/spinlab/session_manager.py` (wrap snapshot in `start_practice` + `start_hyper_play`; add logging to `_take_session_snapshot`)
- Modify: `tests/unit/test_session_manager_snapshot.py` (add real-baseline test + rollback test)

- [ ] **Step 1: Read current snapshot code**

Re-read `python/spinlab/session_manager.py:495-552` (the `_snapshot_inputs`, `_take_session_snapshot`, `_clear_session_snapshot`, `start_practice` methods) and `:581-606` (`start_hyper_play`) to confirm the current sequence.

Re-read `tests/unit/test_session_manager_snapshot.py` end-to-end to understand the existing FakeState pattern.

- [ ] **Step 2: Add `SnapshotFailedError` to errors.py**

Append to `python/spinlab/errors.py`:

```python
class SnapshotFailedError(ActionError):
    """The practice/hyper-play session snapshot could not be computed.

    Raised by SessionManager.start_practice/start_hyper_play when the
    baseline snapshot build raises. The session is rolled back to IDLE
    before this is raised, so the caller can safely retry.
    """
    http_code = 500
    detail = "snapshot_failed"
```

- [ ] **Step 3: Write the failing rollback test**

Append to `tests/unit/test_session_manager_snapshot.py`:

```python
def test_take_session_snapshot_logs_segment_count_on_success(caplog):
    """Successful snapshot capture emits an INFO log with the segment count."""
    import logging
    sm = _make_sm_with_segments(["s0", "s1", "s2"])
    with caplog.at_level(logging.INFO, logger="spinlab.session_manager"):
        sm._take_session_snapshot()  # type: ignore[attr-defined]
    msgs = [r.getMessage() for r in caplog.records]
    assert any("snapshot captured" in m and "n_segments=3" in m for m in msgs), msgs


def test_take_session_snapshot_logs_warning_and_clears_on_failure(caplog):
    """If _snapshot_inputs raises, the snapshot must be cleared to None and
    the failure logged at WARNING (not silently swallowed)."""
    import logging
    sm = _make_sm_with_segments(["s0"])
    sm.practice_session_snapshot = "previous"  # type: ignore[assignment]

    def boom():
        raise RuntimeError("simulated DB outage")

    sm._snapshot_inputs = boom  # type: ignore[assignment]

    with caplog.at_level(logging.WARNING, logger="spinlab.session_manager"):
        sm._take_session_snapshot()  # type: ignore[attr-defined]
    assert sm.practice_session_snapshot is None
    msgs = [r.getMessage() for r in caplog.records]
    assert any("snapshot capture failed" in m for m in msgs), msgs


def test_take_session_snapshot_real_baseline_exercises_closed_form(monkeypatch, tmp_path):
    """Real-baseline path: build a SamplerState through process_event (the
    actual event-driven update path) and assert ONLY what's deterministic.

    Per `feedback_outliers_highlight_not_remove` / Andrew's "don't fudge
    numbers" guidance: floor_ms is a pure min-reduction over clean_tail_ms
    of completed non-invalidated episodes (deterministic — pin it).
    expected_episode_ms and death_rate flow through EMA accumulators with
    a specific ALPHA_GRID; their exact values are NOT pinned here — assert
    only that they're non-None (gate cleared) and in their valid range.
    A specific-value pin would require hand-computing the alpha=0 EMA from
    ALPHA_GRID[DEFAULT_FAST_IDX], which is brittle if the grid changes."""
    import time

    from spinlab.db import Database
    from spinlab.estimators.em_suite_sampler import SamplerState, process_event
    from spinlab.models import AttemptOutcome, AttemptSource, EventAttempt
    from spinlab.session_manager import SessionManager
    from spinlab.system_state import SystemState

    db = Database(tmp_path / "sm.db")
    db.upsert_game("g", "Game", "any%")

    sm = SessionManager.__new__(SessionManager)
    sm.practice_session_snapshot = None
    sm.state = SystemState()
    sm.state.game_id = "g"

    # Build a real SamplerState above the prediction gate
    # (n_successes >= 2 AND n_deaths >= 2 AND n_attempts_total >= 2 per
    # em_suite_sampler._gate_passes at line ~310).
    state = SamplerState()
    sess = "sess-test"
    for i, (outcome, time_ms) in enumerate([
        (AttemptOutcome.SURVIVED, 12_000),
        (AttemptOutcome.SURVIVED, 13_500),
        (AttemptOutcome.SURVIVED, 12_800),
        (AttemptOutcome.DIED, 8_000),
        (AttemptOutcome.DIED, 9_500),
        (AttemptOutcome.DIED, 8_400),
    ]):
        event = EventAttempt(
            segment_id="s0",
            episode_id=f"ep-{i}",
            outcome=outcome,
            time_ms=time_ms,
            session_id=sess,
            source=AttemptSource.PRACTICE,
        )
        state = process_event(state, event)
    # Sanity: did we actually clear the gate?
    assert state.n_successes >= 2 and state.n_deaths >= 2

    # Episodes are AttemptRow shape (TypedDict total=True). Three completed
    # non-invalidated episodes with clean_tail_ms set; floor_ms = min(those).
    episodes = [
        {"segment_id": "s0", "completed": 1, "time_ms": 12_000, "deaths": 0,
         "clean_tail_ms": 11_500, "created_at": "2026-06-03T00:00:00", "invalidated": 0},
        {"segment_id": "s0", "completed": 1, "time_ms": 13_500, "deaths": 1,
         "clean_tail_ms": 12_800, "created_at": "2026-06-03T00:00:01", "invalidated": 0},
        {"segment_id": "s0", "completed": 1, "time_ms": 12_800, "deaths": 0,
         "clean_tail_ms": 11_900, "created_at": "2026-06-03T00:00:02", "invalidated": 0},
    ]
    sm._snapshot_inputs = lambda: [("s0", state, episodes)]  # type: ignore[attr-defined]

    monkeypatch.setattr(time, "time", lambda: 1_717_000_000.0)
    sm._take_session_snapshot()  # type: ignore[attr-defined]

    snap = sm.practice_session_snapshot
    assert snap is not None
    assert snap.started_at == 1_717_000_000.0
    base = snap.segments["s0"]
    # DETERMINISTIC: floor_ms = min(clean_tail_ms) over completed,
    # non-invalidated episodes = min(11_500, 12_800, 11_900) = 11_500.
    assert base.floor_ms == 11_500.0
    # STRUCTURAL: above-gate so EMA-derived values are non-None.
    # Don't pin specific EMA values — they depend on ALPHA_GRID order.
    assert base.expected_episode_ms is not None
    assert base.expected_episode_ms > 0.0
    assert 0.0 <= base.death_rate <= 1.0


@pytest.mark.asyncio
async def test_start_practice_rolls_back_on_snapshot_failure(tmp_path, monkeypatch):
    """If _take_session_snapshot raises inside start_practice, the session
    must roll back: mode=IDLE, practice_session=None, practice_task cancelled,
    and SnapshotFailedError surfaces to the caller."""
    from tests.conftest import FakeEmuBackend

    from spinlab.db import Database
    from spinlab.errors import SnapshotFailedError
    from spinlab.models import Mode
    from spinlab.session_manager import SessionManager

    db = Database(tmp_path / "sm.db")
    db.upsert_game("g", "Game", "any%")
    emu = FakeEmuBackend(connected=True)
    sm = SessionManager(db=db, emu=emu, rom_dir=None, default_category="any%")
    sm.game_id = "g"
    sm.mode = Mode.IDLE

    def boom():
        raise RuntimeError("synthetic snapshot crash")

    monkeypatch.setattr(sm, "_snapshot_inputs", boom)

    with pytest.raises(SnapshotFailedError):
        await sm.start_practice()

    assert sm.mode == Mode.IDLE
    assert sm.practice_session is None
    assert sm.practice_task is None
```

(`pytest` import already at top of file from existing tests — confirm before re-importing.)

- [ ] **Step 4: Run the tests to verify they fail**

Run: `python -m pytest tests/unit/test_session_manager_snapshot.py -k "logs_segment_count or logs_warning or real_baseline or rolls_back" -v`
Expected: all four FAIL.

- [ ] **Step 5: Add logging + try/except to `_take_session_snapshot`**

In `python/spinlab/session_manager.py`, replace `_take_session_snapshot` (currently at lines 515-525) with:

```python
    def _take_session_snapshot(self) -> None:
        """Capture an in-memory baseline of every active segment + the route
        aggregate. Called from practice/hyper-play start.

        On failure: log WARNING, clear snapshot to None, re-raise so the
        caller (start_practice/start_hyper_play) can roll back the session.
        Silent failure would leave the live view emitting all-None diffs
        with no observable cause."""
        import time as _time

        from spinlab.estimators.session_snapshot import snapshot_from_segments

        try:
            inputs = self._snapshot_inputs()
            self.practice_session_snapshot = snapshot_from_segments(
                started_at=_time.time(),
                segments=inputs,
            )
            logger.info(
                "snapshot captured: n_segments=%d", len(inputs),
            )
        except Exception:
            self.practice_session_snapshot = None
            logger.warning("snapshot capture failed", exc_info=True)
            raise
```

- [ ] **Step 6: Add rollback to `start_practice`**

In `python/spinlab/session_manager.py`, modify `start_practice` (lines 530-552) to wrap the snapshot call in try/except:

```python
    async def start_practice(self) -> ActionResult:
        from .errors import SnapshotFailedError

        if self.capture.has_paused_run:
            raise DraftPendingError()
        if self.practice_session and self.practice_session.is_running:
            raise AlreadyRunningError()
        if not self.emu.is_connected:
            raise NotConnectedError()
        if self.mode == Mode.REFERENCE:
            self._clear_ref_and_idle()

        from .practice import PracticeSession
        ps = PracticeSession(
            emu=self.emu, db=self.db, game_id=self.require_game(),
            death_penalty_ms=self.capture.condition_registry.death_penalty_ms,
            on_attempt=lambda _: asyncio.create_task(self._notify_sse()),
        )
        self.practice_session = ps
        self.practice_task = asyncio.create_task(ps.run_loop())
        self.practice_task.add_done_callback(self._on_practice_done)
        self.mode = Mode.PRACTICE
        try:
            self._take_session_snapshot()
        except Exception as exc:
            # Roll back the half-started session so the caller can retry.
            # The done-callback would also clear mode eventually, but the
            # route needs a clean IDLE *now* and a typed error to surface.
            ps.is_running = False
            self.practice_task.cancel()
            self.practice_session = None
            self.practice_task = None
            self.mode = Mode.IDLE
            self._clear_session_snapshot()
            raise SnapshotFailedError() from exc
        await self._notify_sse()
        return ActionResult(status=Status.STARTED, session_id=ps.session_id)
```

Apply the equivalent change to `start_hyper_play` (lines 581-606):

```python
    async def start_hyper_play(self) -> ActionResult:
        from .errors import SnapshotFailedError

        if self.capture.has_paused_run:
            raise DraftPendingError()
        if self.hyper_play_session and self.hyper_play_session.is_running:
            raise AlreadyRunningError()
        if not self.emu.is_connected:
            raise NotConnectedError()
        if self.mode == Mode.REFERENCE:
            self._clear_ref_and_idle()

        from .hyper_play import HyperPlaySession
        try:
            sr = HyperPlaySession(
                emu=self.emu, db=self.db, game_id=self.require_game(),
                on_event=lambda _: asyncio.create_task(self._notify_sse()),
            )
        except ValueError:
            raise MissingSaveStatesError()

        self.hyper_play_session = sr
        self.hyper_play_task = asyncio.create_task(sr.run_loop())
        self.hyper_play_task.add_done_callback(self._on_hyper_play_done)
        self.mode = Mode.HYPER_PLAY
        try:
            self._take_session_snapshot()
        except Exception as exc:
            sr.is_running = False
            self.hyper_play_task.cancel()
            self.hyper_play_session = None
            self.hyper_play_task = None
            self.mode = Mode.IDLE
            self._clear_session_snapshot()
            raise SnapshotFailedError() from exc
        await self._notify_sse()
        return ActionResult(status=Status.STARTED, session_id=sr.session_id)
```

- [ ] **Step 7: Run the new tests to verify they pass**

Run: `python -m pytest tests/unit/test_session_manager_snapshot.py -v`
Expected: PASS (all original tests + the four new ones).

- [ ] **Step 8: Run the fast suite**

Run: `python -m pytest -m "not emulator" -q`
Expected: all green.

- [ ] **Step 9: Run pyright + ruff on changed files**

Run: `npx pyright python/spinlab/errors.py python/spinlab/session_manager.py tests/unit/test_session_manager_snapshot.py`
Expected: no new errors.

Run: `ruff check python/spinlab/errors.py python/spinlab/session_manager.py tests/unit/test_session_manager_snapshot.py`
Expected: `All checks passed!`

- [ ] **Step 10: Commit**

```bash
git add python/spinlab/errors.py python/spinlab/session_manager.py tests/unit/test_session_manager_snapshot.py
git commit -m "$(cat <<'EOF'
fix(session-manager): wrap snapshot capture in try/except with rollback + logging

_take_session_snapshot was silent on both success and failure; start_practice
and start_hyper_play called it unprotected after flipping mode. On exception
the route returned 500 but mode was already PRACTICE and the async task was
running — a wedge the user couldn't reason about.

Add INFO log on success (n_segments), WARNING + exc_info on failure. Wrap
the call in start_practice/start_hyper_play: on failure, cancel the task,
clear practice/hyper_play_session, reset mode to IDLE, raise the new typed
SnapshotFailedError(500). Backed by a real-baseline test that exercises
the closed-form path through snapshot_from_segments (FakeState n=0/n=0 in
the existing tests stayed below the prediction gate).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task E: PracticeSession Scheduler DI

**Goal:** `PracticeSession.__init__` currently constructs `Scheduler(db, game_id)` at line 58, while `SessionManager` lazy-creates its own at `get_scheduler()`. Two instances, same DB. The facade is the obvious injection point: `SessionManager.start_practice()` passes its scheduler to `PracticeSession`. Eliminates split-brain.

**Files:**
- Modify: `python/spinlab/practice.py:41-58` (`__init__`)
- Modify: `python/spinlab/session_manager.py:541-545` (PracticeSession construction in `start_practice`)
- Modify: `tests/unit/test_practice.py` (or wherever PracticeSession is constructed in tests — find via grep)

- [ ] **Step 1: Read PracticeSession current `__init__` + find test constructors**

Read `python/spinlab/practice.py:38-100` to see the full `__init__` signature.

Run: `grep -rn "PracticeSession(" tests/ python/spinlab/ --include="*.py"`
to find every construction site. Each one will need updating (or accepting the new optional param without change if we make scheduler= optional with a default).

- [ ] **Step 2: Decide signature shape**

**Decision:** Make `scheduler` a required kwarg (not optional). Tests that construct PracticeSession should be explicit about which Scheduler they're using. An optional default would re-create the split-brain problem this task is solving. The test files will need updating.

- [ ] **Step 3: Write the failing test**

Find or create `tests/unit/test_practice.py`. Append:

```python
def test_practice_session_uses_injected_scheduler(tmp_path):
    """PracticeSession must accept a Scheduler and not construct its own."""
    from tests.conftest import FakeEmuBackend

    from spinlab.db import Database
    from spinlab.practice import PracticeSession
    from spinlab.scheduler import Scheduler

    db = Database(tmp_path / "p.db")
    db.upsert_game("g", "Game", "any%")
    emu = FakeEmuBackend(connected=True)
    scheduler = Scheduler(db, "g")

    ps = PracticeSession(
        emu=emu, db=db, game_id="g",
        death_penalty_ms=3200,
        scheduler=scheduler,
    )
    assert ps.scheduler is scheduler  # same instance — no construction
```

- [ ] **Step 4: Run the test to verify it fails**

Run: `python -m pytest tests/unit/test_practice.py::test_practice_session_uses_injected_scheduler -v`
Expected: FAIL — `TypeError: PracticeSession.__init__() got an unexpected keyword argument 'scheduler'` OR test asserts `ps.scheduler is not scheduler`.

- [ ] **Step 5: Modify `PracticeSession.__init__` to accept the scheduler**

In `python/spinlab/practice.py`, change the `__init__` signature and body (current lines 41-58):

```python
    def __init__(
        self,
        emu: "EmuBackend",
        db: "Database",
        game_id: str,
        *,
        scheduler: Scheduler,
        auto_advance_delay_ms: int = 1000,
        death_penalty_ms: int = 3200,
        on_attempt: Callable | None = None,
        session_id: str | None = None,
    ) -> None:
        self.emu = emu
        self.db = db
        self.game_id = game_id
        self.auto_advance_delay_ms = auto_advance_delay_ms
        self.death_penalty_ms = death_penalty_ms
        self.on_attempt = on_attempt

        self.scheduler = scheduler
        # ... rest unchanged
```

Key changes:
- `*` introduces kwarg-only after positional `emu`, `db`, `game_id`.
- `scheduler: Scheduler` is required (no default).
- Drop `self.scheduler = Scheduler(db, game_id)` — replaced with `self.scheduler = scheduler`.

The `from .scheduler import Scheduler` import at the top of the file stays (it's still the type annotation).

- [ ] **Step 6: Run the test to verify it passes**

Run: `python -m pytest tests/unit/test_practice.py::test_practice_session_uses_injected_scheduler -v`
Expected: PASS.

- [ ] **Step 7: Update SessionManager.start_practice to inject scheduler**

In `python/spinlab/session_manager.py`, modify the `PracticeSession(...)` construction inside `start_practice` (currently around lines 541-545 after Task D's modifications):

```python
        ps = PracticeSession(
            emu=self.emu, db=self.db, game_id=self.require_game(),
            scheduler=self.get_scheduler(),
            death_penalty_ms=self.capture.condition_registry.death_penalty_ms,
            on_attempt=lambda _: asyncio.create_task(self._notify_sse()),
        )
```

- [ ] **Step 8: Update every other PracticeSession test construction site**

Run: `grep -rn "PracticeSession(" tests/ --include="*.py"` and update each call site to pass `scheduler=Scheduler(db, "<game_id>")`. The exact list will surface from grep — expect 3-8 sites in tests/unit/.

- [ ] **Step 9: Run the fast suite**

Run: `python -m pytest -m "not emulator" -q`
Expected: all green. Any FAIL with `TypeError: missing 1 required keyword-only argument: 'scheduler'` is an unupdated test site — find and fix.

- [ ] **Step 10: Run pyright + ruff on changed files**

Run: `npx pyright python/spinlab/practice.py python/spinlab/session_manager.py tests/unit/test_practice.py`
Expected: no new errors.

Run: `ruff check python/spinlab/practice.py python/spinlab/session_manager.py tests/unit/test_practice.py`
Expected: `All checks passed!`

- [ ] **Step 11: Commit**

```bash
git add python/spinlab/practice.py python/spinlab/session_manager.py tests/
git commit -m "$(cat <<'EOF'
refactor(practice): inject Scheduler instead of constructing one in __init__

PracticeSession was building Scheduler(db, game_id) in __init__ while
SessionManager lazy-created its own at get_scheduler() — split-brain on
the same DB. Make scheduler a required kwarg; SessionManager.start_practice
now passes self.get_scheduler() so both paths share one instance.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task F: Document ConditionRegistry install invariant + add runtime assertion

**Goal:** `ReferenceController.__init__` (capture/reference.py:71-72) creates an empty `ConditionRegistry()`, which `SessionManager.install_condition_registry` (session_manager.py:272-282) replaces post-construction via `self.capture.set_condition_registry(registry)`. The invariant "ROM-load → install_condition_registry runs before any segment recording starts" is enforced today only because `route_event` serializes event handlers — but it's not type-enforced and not documented. Add a runtime assertion + module docstring section to surface a violation if the invariant ever breaks (e.g., a future change moves recording-start into a parallel code path).

**Files:**
- Modify: `python/spinlab/capture/reference.py` (add docstring section + assertion in `set_condition_registry`)
- Modify: `tests/unit/test_session_manager_conditions.py` (add assertion-violation test)

- [ ] **Step 1: Re-read the current state**

Read `python/spinlab/capture/reference.py:65-92` and `tests/unit/test_session_manager_conditions.py` end-to-end to see the existing test fixtures.

- [ ] **Step 2: Write the failing test**

Append to `tests/unit/test_session_manager_conditions.py`:

```python
def test_set_condition_registry_raises_if_called_while_recording(tmp_path):
    """The install invariant says: install_condition_registry runs BEFORE
    recording starts (route_event serializes event handlers, so ROM-load
    always lands before reference/start). Replacing the registry mid-record
    would silently mismatch decode shape vs. recorder buffer state.
    Assert to surface a future code path that violates this."""
    from spinlab.capture.reference import ReferenceController
    from spinlab.condition_registry import ConditionRegistry
    from spinlab.db import Database

    db = Database(tmp_path / "ref.db")
    db.upsert_game("g", "Game", "any%")
    rc = ReferenceController(db=db, emu=None)  # type: ignore[arg-type]
    # Simulate active recording state.
    rc.recorder.capture_run_id = "active-run"
    new_registry = ConditionRegistry()  # empty, distinct instance

    import pytest as _pytest
    with _pytest.raises(AssertionError, match="ConditionRegistry"):
        rc.set_condition_registry(new_registry)
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `python -m pytest tests/unit/test_session_manager_conditions.py::test_set_condition_registry_raises_if_called_while_recording -v`
Expected: FAIL (the assertion doesn't exist yet — the test executes without raising).

- [ ] **Step 4: Add the docstring section + assertion**

In `python/spinlab/capture/reference.py`, expand the module docstring at the top to include an Invariants section. Replace the current docstring (lines 1-12) with:

```python
"""ReferenceController — orchestrates reference recording and replay capture.

State model:
- IDLE: no run loaded
- RECORDING: a session is active, recorder is buffering events
- PAUSED: a draft=1 capture_run exists but no active session

Stop is non-destructive: it ends the current session and leaves the run paused.
Resume creates a new session under the existing paused run. Finalize promotes
the draft to saved and activates; event rows are already in attempts from the
recorder writing them as each segment closed.

Invariants:
- `ConditionRegistry` is installed via `set_condition_registry()` exactly
  before recording begins. The install path is SessionManager._handle_rom_info
  → install_condition_registry → set_condition_registry, which runs under the
  same `route_event` await as any subsequent reference-start. Replacing the
  registry mid-record would silently mismatch decode shapes; an assertion in
  set_condition_registry surfaces a violation if a future code path breaks
  this ordering.
"""
```

Modify `set_condition_registry` at line 89-91 to:

```python
    def set_condition_registry(self, registry: ConditionRegistry) -> None:
        # See module docstring "Invariants": install must run before any
        # recording starts. A registry swap mid-record desyncs decoder
        # state and corrupts the buffered events.
        assert not self.is_recording, (
            "ConditionRegistry replaced mid-record — install must precede "
            "reference-start. See capture/reference.py module docstring."
        )
        self.condition_registry = registry
        self.recorder.set_condition_registry(registry)
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `python -m pytest tests/unit/test_session_manager_conditions.py::test_set_condition_registry_raises_if_called_while_recording -v`
Expected: PASS.

- [ ] **Step 6: Run the existing conditions tests to confirm no regression**

Run: `python -m pytest tests/unit/test_session_manager_conditions.py -v`
Expected: all PASS (the original tests don't trigger the assertion because they install before any recording).

- [ ] **Step 7: Run the fast suite**

Run: `python -m pytest -m "not emulator" -q`
Expected: all green.

- [ ] **Step 8: Run pyright + ruff on changed files**

Run: `npx pyright python/spinlab/capture/reference.py tests/unit/test_session_manager_conditions.py`
Expected: no new errors.

Run: `ruff check python/spinlab/capture/reference.py tests/unit/test_session_manager_conditions.py`
Expected: `All checks passed!`

- [ ] **Step 9: Commit**

```bash
git add python/spinlab/capture/reference.py tests/unit/test_session_manager_conditions.py
git commit -m "$(cat <<'EOF'
docs(reference): document ConditionRegistry install invariant + assert at set time

ReferenceController.set_condition_registry was called post-construction with
no type-level guarantee that recording hadn't started. The ordering is enforced
in practice by route_event serializing event handlers (ROM-load lands before
reference-start), but a future parallel code path could break it silently —
the decoder would desync and the recorder would buffer mismatched events.

Add an Invariants section to the module docstring and assert
`not self.is_recording` at the top of set_condition_registry so a violation
surfaces immediately instead of later as a "why are my segments wrong" mystery.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Plan-complete verification

Before declaring CF1 complete and inviting Phase 10 (finalize-branch):

- [ ] **Step 1: Run the full suite (per CLAUDE.md "Merging Branches")**

Run: `python -m pytest`
Expected: all green, 0 skipped (or only `@pytest.mark.skipif`'d cases with documented reasons that Andrew has accepted).

Per CLAUDE.md: "Skips count as failures." If any emulator test skips with `ra_harness launch failed`, surface to user — do not silently pass.

- [ ] **Step 2: Run the stress check on snapshot-related tests**

Per `feedback_stress_test_flakes`, the snapshot lifecycle work touched async/task code; run 15+ times to confirm no flake introduced:

Run: `for i in $(seq 1 15); do python -m pytest tests/unit/test_session_manager_snapshot.py tests/unit/test_session_manager.py -q || break; done`
(On Windows PowerShell: `1..15 | ForEach-Object { python -m pytest tests/unit/test_session_manager_snapshot.py tests/unit/test_session_manager.py -q; if (-not $?) { break } }`)
Expected: 15 clean runs in a row.

- [ ] **Step 3: Verify routes/system.py no longer reaches into private SessionManager state**

Run: `grep -n "session\._\|session\.mode = \|session\.scheduler = " python/spinlab/routes/system.py python/spinlab/routes/practice.py`
Expected: zero matches (or only matches that are commented out / inside docstrings).

- [ ] **Step 4: Verify pyright + ruff baseline unchanged**

Run: `npx pyright python/ 2>&1 | tail -3`
Expected: error count unchanged or lower vs the 261 baseline. If higher, identify the file and either fix or document as an accepted regression.

Run: `ruff check python/`
Expected: `All checks passed!`

---

## Self-review checklist (run before declaring plan ready)

**Spec coverage:**
- [x] M1(a) — `_take_session_snapshot` zero logging → Task D adds INFO/WARNING.
- [x] M1(b) — `start_practice` unprotected snapshot → Task D wraps in try/except with full rollback.
- [x] M1(c) — Tests stay below prediction gate → Task D adds `test_take_session_snapshot_real_baseline_exercises_closed_form` with real `SamplerState`.
- [x] M2 — `PracticeSession` constructs own Scheduler → Task E injects via required kwarg.
- [x] M3 — Routes mutate `session.mode` / call private `_clear_ref_and_idle` → Tasks B + C absorb these into `start_cold_fill()` and `reset_data()`; Task A covers the third (`practice_invalidate` route).
- [x] M5 — ConditionRegistry mutable post-construction → Task F documents invariant + asserts violation.

**Placeholder scan:** No `TBD`, `TODO`, `implement later`, or "similar to Task N" — each task has full code blocks.

**Type consistency:**
- `WrongModeError` defined Task B step 2; used Task B step 5, Task B step 7 (route response), Task B step 3 (test).
- `SnapshotFailedError` defined Task D step 2; used Task D step 6 (raise), Task D step 3 (test).
- `scheduler` kwarg added Task E step 5; used Task E step 7 (SessionManager passes it).

All consistent.
