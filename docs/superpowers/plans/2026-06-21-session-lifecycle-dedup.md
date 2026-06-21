# SessionManager Lifecycle Dedup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the duplicated practice/hyper-play session-lifecycle code in `python/spinlab/session_manager.py` by extracting three shared helpers, with zero observable behavior change.

**Architecture:** This is a pure refactor under existing test coverage — NOT a feature. The regression net already exists in `tests/unit/test_session_manager.py` and `tests/unit/test_session_manager_snapshot.py` (crash-clears-snapshot, clean-completion-freezes-snapshot, rolls-back-on-snapshot-failure, blocked-by-draft/not-connected/already-running, for both practice and hyper-play). Each task extracts one helper, keeps every public AND `_on_*_done` method name intact (the tests call `_on_practice_done`/`_on_hyper_play_done` directly), and uses the existing tests as the green gate. No new test is written to "prove" the refactor — the existing tests passing unchanged IS the proof. We only ADD one tiny assertion-tightening test in Task 1 to lock the new shared method's contract.

**Tech Stack:** Python 3.11, asyncio, pytest. No new dependencies.

## Global Constraints

- **Bit-identical behavior.** Every refactor step must preserve observable behavior exactly: rollback nulls the correct `*_session`/`*_task` fields, sets `Mode.IDLE`, clears the snapshot, and raises `SnapshotFailedError() from exc`; done-callbacks freeze on clean finish and clear on crash/cancel; log labels stay `"practice"` / `"hyper_play"`.
- **No `setattr`-by-string magic.** Per project legibility rules (`feedback_legibility_no_shortcuts`): the differing parts (session construction, which slot is written) stay explicit in the public methods; only the shared envelope is factored. Pass an explicit rollback closure and the current session object — do not reach fields by computed attribute name.
- **No magic numbers / no fudge factors** (project CLAUDE.md) — N/A here (no constants introduced), but keep it in mind.
- **The existing tests are not to be loosened.** If a test must change, that is a signal the refactor changed behavior — stop and reconsider.
- **Merge gate:** the FULL suite (`python -m pytest`, including emulator + frontend smoke) must pass before merge — not `-m "not emulator"`.

---

### Task 1: Extract `_on_session_done`, make the two done-callbacks thin wrappers

**Files:**
- Modify: `python/spinlab/session_manager.py:736-754` (`_on_practice_done`) and `:829-848` (`_on_hyper_play_done`)
- Test: `tests/unit/test_session_manager_snapshot.py` (existing tests at `:70`, `:94`, `:113`, `:135` are the regression net; add one new test)

**Interfaces:**
- Produces: `SessionManager._on_session_done(self, task: asyncio.Task, mode: Mode, label: str) -> None` — the shared done-callback body. `_on_practice_done(task)` and `_on_hyper_play_done(task)` remain as public-surface wrappers (the snapshot tests call them by name) that delegate to it.

The two current bodies are word-for-word identical except the `logger.error` label and the `Mode` constant guarding the freeze/clear. Current `_on_practice_done` (lines 736-754):

```python
    def _on_practice_done(self, task: asyncio.Task) -> None:
        clean = False
        if task.cancelled():
            pass  # abnormal teardown (rollback/cancel) — not a clean finish
        else:
            exc = task.exception()
            if exc is not None:
                logger.error("practice task crashed", exc_info=exc)
            else:
                clean = True
        if self.mode == Mode.PRACTICE:
            self.mode = Mode.IDLE
            # A clean finish freezes the snapshot so the idle view persists, the
            # same as a user stop; a crash/cancel clears it (no stale baseline).
            if clean:
                self._freeze_session_snapshot()
            else:
                self._clear_session_snapshot()
            asyncio.create_task(self._notify_sse())
```

`_on_hyper_play_done` (lines 829-848) is identical but logs `"hyper_play task crashed"` and guards on `Mode.HYPER_PLAY`.

- [ ] **Step 1: Add a contract test for the new shared method**

Add to `tests/unit/test_session_manager_snapshot.py` (after the existing `test_on_hyper_play_done_clean_completion_freezes_snapshot` at line ~154). This pins that the shared method only acts when the live mode matches the passed mode (a guard the wrappers rely on):

```python
def test_on_session_done_ignores_when_mode_already_changed(monkeypatch):
    """_on_session_done must no-op if the live mode no longer matches the
    session that finished — e.g. the user already switched to REFERENCE. This
    guards against a late done-callback clobbering a newer mode."""
    from spinlab.models import Mode

    sm = _make_sm_with_segments(["s0"])
    sm.mode = Mode.REFERENCE  # live mode differs from the finishing session
    sm._take_session_snapshot()  # type: ignore[attr-defined]
    assert sm.practice_session_snapshot is not None

    monkeypatch.setattr(sm, "_notify_sse", lambda: None)
    import asyncio as _asyncio
    monkeypatch.setattr(_asyncio, "create_task", lambda coro: None)

    sm._on_session_done(_fake_task(exc=RuntimeError("boom")), Mode.PRACTICE, "practice")

    # Mode untouched, snapshot untouched — the stale callback did nothing.
    assert sm.mode == Mode.REFERENCE
    assert sm.practice_session_snapshot is not None
```

- [ ] **Step 2: Run the new test to verify it fails**

Run: `python -m pytest tests/unit/test_session_manager_snapshot.py::test_on_session_done_ignores_when_mode_already_changed -v`
Expected: FAIL with `AttributeError: 'SessionManager' object has no attribute '_on_session_done'`

- [ ] **Step 3: Extract the shared method and rewrite both callbacks as wrappers**

Replace the entire `_on_practice_done` body (lines 736-754) with the shared method plus a thin wrapper:

```python
    def _on_session_done(self, task: asyncio.Task, mode: Mode, label: str) -> None:
        """Shared done-callback body for practice and hyper-play. A clean finish
        (not cancelled, no exception) FREEZES the snapshot so the idle view
        persists like a user stop; a crash/cancel CLEARS it (no stale baseline).
        No-ops when the live mode no longer matches the finishing session."""
        clean = False
        if task.cancelled():
            pass  # abnormal teardown (rollback/cancel) — not a clean finish
        else:
            exc = task.exception()
            if exc is not None:
                logger.error("%s task crashed", label, exc_info=exc)
            else:
                clean = True
        if self.mode == mode:
            self.mode = Mode.IDLE
            if clean:
                self._freeze_session_snapshot()
            else:
                self._clear_session_snapshot()
            asyncio.create_task(self._notify_sse())

    def _on_practice_done(self, task: asyncio.Task) -> None:
        self._on_session_done(task, Mode.PRACTICE, "practice")
```

Then replace the entire `_on_hyper_play_done` body (now shifted from lines 829-848) with just the wrapper:

```python
    def _on_hyper_play_done(self, task: asyncio.Task) -> None:
        self._on_session_done(task, Mode.HYPER_PLAY, "hyper_play")
```

Note: the log message changes from the literal `"practice task crashed"` to the `"%s task crashed"` format with `label`. The rendered message is identical (`"practice task crashed"` / `"hyper_play task crashed"`). No test asserts this exact string, but verify in Step 4 that nothing regressed.

- [ ] **Step 4: Run the done-callback regression tests + the new test**

Run: `python -m pytest tests/unit/test_session_manager_snapshot.py -v -k "done or session_done"`
Expected: PASS — all of `test_on_practice_done_crash_clears_snapshot`, `test_on_hyper_play_done_crash_clears_snapshot`, `test_on_practice_done_clean_completion_freezes_snapshot`, `test_on_hyper_play_done_clean_completion_freezes_snapshot`, and the new `test_on_session_done_ignores_when_mode_already_changed`.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/session_manager.py tests/unit/test_session_manager_snapshot.py
git commit -m "refactor(session): extract shared _on_session_done done-callback"
```

---

### Task 2: Extract `_preflight_session_start` and `_snapshot_or_rollback` from the start methods

**Files:**
- Modify: `python/spinlab/session_manager.py` — `start_practice` (currently lines 680-734) and `start_hyper_play` (currently lines 791-827). NOTE: line numbers shift after Task 1 (the done-callback block shrank by ~14 lines); locate the methods by name, not by absolute line.
- Test: `tests/unit/test_session_manager.py` (`test_start_practice_blocked_by_draft` `:479`, `test_start_practice_blocked_by_not_connected` `:486`, `test_start_practice_blocked_when_already_running` `:493`, the hyper-play `test_start_blocked_by_draft` `:577`, `test_start_blocked_by_not_connected` `:584`, `test_start_missing_save_states` `:591`) and `tests/unit/test_session_manager_snapshot.py::test_start_practice_rolls_back_on_snapshot_failure` `:442` are the regression net.

**Interfaces:**
- Consumes: nothing new from Task 1.
- Produces:
  - `SessionManager._preflight_session_start(self, running_session) -> None` — runs the four shared pre-checks. `running_session` is the current `self.practice_session` or `self.hyper_play_session` (either may be `None`). Raises `DraftPendingError` / `AlreadyRunningError` / `NotConnectedError`; clears REFERENCE mode in place.
  - `SessionManager._snapshot_or_rollback(self, rollback: Callable[[], None]) -> None` — calls `_take_session_snapshot()`; on any exception runs `rollback()`, sets `Mode.IDLE`, clears the snapshot, and raises `SnapshotFailedError() from exc`.

The four current pre-checks in BOTH methods (only the session field differs):

```python
        if self.capture.has_paused_run:
            raise DraftPendingError()
        if self.practice_session and self.practice_session.is_running:   # hyper: self.hyper_play_session
            raise AlreadyRunningError()
        if not self.emu.is_connected:
            raise NotConnectedError()
        if self.mode == Mode.REFERENCE:
            self._clear_ref_and_idle()
```

The current snapshot/rollback tail in `start_practice` (lines 720-732):

```python
        try:
            self._take_session_snapshot()
        except Exception as exc:
            # Roll back the half-started session so the caller can retry.
            ps.is_running = False
            self.practice_task.cancel()
            self.practice_session = None
            self.practice_task = None
            self.mode = Mode.IDLE
            self._clear_session_snapshot()
            raise SnapshotFailedError() from exc
```

`start_hyper_play` (lines 816-825) is identical but nulls `sr`/`hyper_play_session`/`hyper_play_task`.

- [ ] **Step 1: Run the existing start-path regression tests to confirm green baseline**

Run: `python -m pytest tests/unit/test_session_manager.py tests/unit/test_session_manager_snapshot.py -v -k "start or rolls_back"`
Expected: PASS (this is the pre-refactor baseline — these must stay green after the edit).

- [ ] **Step 2: Add the two helpers**

Add these two methods to `SessionManager` (place them just before `start_practice`). `SnapshotFailedError` is currently imported lazily inside each start method; import it lazily here too to match the existing pattern:

```python
    def _preflight_session_start(self, running_session) -> None:
        """Shared pre-checks for starting an interactive (practice/hyper-play)
        session. Raises a typed error if start is not currently allowed, and
        clears a lingering REFERENCE mode so the new session starts from IDLE."""
        if self.capture.has_paused_run:
            raise DraftPendingError()
        if running_session is not None and running_session.is_running:
            raise AlreadyRunningError()
        if not self.emu.is_connected:
            raise NotConnectedError()
        if self.mode == Mode.REFERENCE:
            self._clear_ref_and_idle()

    def _snapshot_or_rollback(self, rollback: Callable[[], None]) -> None:
        """Capture the start-of-session snapshot; on failure run the caller's
        rollback, return to a clean IDLE, and surface a typed SnapshotFailedError.
        The route needs a clean IDLE *now* and a typed error to retry — the
        done-callback would eventually clear mode but not in time for the caller."""
        from .errors import SnapshotFailedError

        try:
            self._take_session_snapshot()
        except Exception as exc:
            rollback()
            self.mode = Mode.IDLE
            self._clear_session_snapshot()
            raise SnapshotFailedError() from exc
```

- [ ] **Step 3: Rewrite `start_practice` to use both helpers**

Replace the pre-check block (the four `if` statements after the lazy `SnapshotFailedError` import) with a call to `_preflight_session_start`, and replace the `try/except` tail with a call to `_snapshot_or_rollback`. The method becomes (keep the existing grind logic and PracticeSession construction verbatim — only the bracketed envelope changes):

```python
    async def start_practice(self, grind_segment_id: str | None = None) -> ActionResult:
        self._preflight_session_start(self.practice_session)

        game_id = self.require_game()
        if grind_segment_id is not None and not self._grind_segment_practicable(
            game_id, grind_segment_id
        ):
            raise GrindSegmentNotPracticableError()

        from .practice import (
            DEFAULT_AUTO_ADVANCE_MS,
            GRIND_RELOAD_DELAY_MS,
            PracticeSession,
        )
        reload_delay = (
            GRIND_RELOAD_DELAY_MS if grind_segment_id is not None
            else DEFAULT_AUTO_ADVANCE_MS
        )
        ps = PracticeSession(
            emu=self.emu, db=self.db, game_id=game_id,
            scheduler=self.get_scheduler(),
            auto_advance_delay_ms=reload_delay,
            death_penalty_ms=self.capture.condition_registry.death_penalty_ms,
            on_attempt=lambda _: asyncio.create_task(self._notify_sse()),
            on_segment_load=lambda _: asyncio.create_task(self._notify_sse()),
            grind_segment_id=grind_segment_id,
        )
        self.practice_session = ps
        self.practice_task = asyncio.create_task(ps.run_loop())
        self.practice_task.add_done_callback(self._on_practice_done)
        self.mode = Mode.PRACTICE

        def _rollback() -> None:
            ps.is_running = False
            self.practice_task.cancel()
            self.practice_session = None
            self.practice_task = None

        self._snapshot_or_rollback(_rollback)
        await self._notify_sse()
        return ActionResult(status=Status.STARTED, session_id=ps.session_id)
```

Note: the top-of-method `from .errors import SnapshotFailedError` line is now removed from `start_practice` (it moved into `_snapshot_or_rollback`).

- [ ] **Step 4: Rewrite `start_hyper_play` to use both helpers**

```python
    async def start_hyper_play(self) -> ActionResult:
        self._preflight_session_start(self.hyper_play_session)

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

        def _rollback() -> None:
            sr.is_running = False
            self.hyper_play_task.cancel()
            self.hyper_play_session = None
            self.hyper_play_task = None

        self._snapshot_or_rollback(_rollback)
        await self._notify_sse()
        return ActionResult(status=Status.STARTED, session_id=sr.session_id)
```

Note: the top-of-method `from .errors import SnapshotFailedError` line is removed from `start_hyper_play` too.

- [ ] **Step 5: Run the start-path regression tests + lint/type-check**

Run: `python -m pytest tests/unit/test_session_manager.py tests/unit/test_session_manager_snapshot.py tests/unit/test_practice.py tests/unit/test_hyper_play_mode.py -v`
Expected: PASS (all start/stop/done/rollback/blocked tests green).

Run: `ruff check python/spinlab/session_manager.py && npx pyright python/spinlab/session_manager.py`
Expected: ruff clean; pyright introduces no NEW errors (compare against the pre-existing tracked count — the lazy import and closures must not add errors). If `MissingSaveStatesError` / `DraftPendingError` / etc. show as unused-import after the edit, that means a code path was dropped — investigate, do not silence.

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/session_manager.py
git commit -m "refactor(session): extract _preflight_session_start + _snapshot_or_rollback"
```

---

### Task 3: Full-suite verification before merge

**Files:** none (verification only).

**Interfaces:** none.

- [ ] **Step 1: Build the frontend (required for the smoke test)**

Run: `cd frontend && npm run build` then return to repo root.
Expected: `✓ built` with no errors.

- [ ] **Step 2: Run the FULL suite (unit + emulator + frontend smoke)**

Run (from repo root): `python -m pytest`
Expected: all pass. The only acceptable warning is the known `_segments_v07/api.py:165` RuntimeWarning (documented cosmetic noise). ZERO failures, ZERO unexpected skips — per project CLAUDE.md, a SKIPPED emulator test counts as a failure (the RA harness self-launches; a launch failure is a bug to surface, not a green light).

- [ ] **Step 3: Confirm the diff is a pure refactor**

Run: `git diff main -- python/spinlab/session_manager.py`
Expected: net line REDUCTION in the start/done region; the only semantic change is the `"%s task crashed"` log format (renders identically). No change to rollback field-nulling, freeze/clear logic, or raised error types. If the diff shows a behavior change, stop.

- [ ] **Step 4: Hand back to /improve Phase 10 (FINALIZE-BRANCH)**

No commit here — Task 3 is a gate. Report the full-suite result so the orchestrator can run the merge step.

---

## Self-Review

**1. Spec coverage:** The arg asked for (a) a shared `_on_session_done(task, mode, label)` — Task 1; (b) a shared start helper parameterizing over the session slot — Task 2 splits this into `_preflight_session_start` (the prechecks) + `_snapshot_or_rollback` (the snapshot/rollback envelope), keeping session construction explicit per the no-magic legibility constraint; (c) bit-identical behavior with the existing tests as the net — every task runs the named regression tests; (d) full emulator suite before merge — Task 3. Covered.

**2. Placeholder scan:** No TBD/TODO/"similar to". Every code step shows full code.

**3. Type consistency:** `_on_session_done(task, mode, label)`, `_preflight_session_start(running_session)`, `_snapshot_or_rollback(rollback)` are referenced consistently across tasks. `Callable` is already imported at `session_manager.py:7`. `Mode`, `Status`, `ActionResult` already imported. `SnapshotFailedError` imported lazily (matching the existing pattern). The two `_on_*_done` and both public `start_*` names are preserved, so the existing tests bind unchanged.
