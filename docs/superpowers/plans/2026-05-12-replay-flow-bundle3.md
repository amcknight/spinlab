# Bundle 3: Replay Flow Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move `replay_total` off `SessionManager` onto `ReferenceController` where the rest of replay state lives (C1); fix the eager mode-flip race in `SessionManager.start_replay` by gating capture-event handlers on `capture.is_recording` instead of `mode in (REFERENCE, REPLAY)` (C3).

**Architecture:** C1 is a 1:1 move with a new `ReferenceController.handle_replay_started(event)` method that owns the field write. C3 changes 5 mode-gated handlers (`_handle_level_entrance`, `_handle_checkpoint`, `_handle_death`, `_handle_spawn`, `_handle_level_exit`) to gate on the authoritative `capture.is_recording` bit. Once those gates are permissive, the 20-line workaround in `start_replay` (eager mode flip + rollback in except) collapses to a normal `_apply_result` call.

**Tech Stack:** Python 3.11+, asyncio, pytest.

---

## File Structure

**Modified:**
- `python/spinlab/capture/reference.py` — add `replay_total: int = 0` field + `handle_replay_started(event)` method; reset in `handle_replay_finished` / `handle_replay_error`
- `python/spinlab/session_manager.py` — delete `self.replay_total` and the eager mode-flip block in `start_replay`; route `_handle_replay_started` through `self.capture.handle_replay_started(event)`; change capture-routing handlers' mode gates to `self.capture.is_recording`
- `python/spinlab/state_builder.py` — read `session.capture.replay_total` instead of `session.replay_total`

**Test additions:**
- `tests/unit/test_session_manager.py` — regression test for the C3 race: capture-events route to the recorder while `is_recording` is True even if mode hasn't flipped yet
- `tests/unit/capture/test_reference.py` (or test_replay.py) — unit test for `ReferenceController.handle_replay_started` setting the field

---

## Conventions

- TDD where there's an assertion to make. The "1:1 field move" tasks need at least one test asserting StateBuilder still produces the same `replay.total` shape — but those tests already exist (integration replay test asserts `replay.total > 0`).
- One task = one commit.
- Run `python -m pytest -m "not emulator"` after each task that touches production code.
- Final task runs full `python -m pytest`.

---

## Phase 1: C1 — Move `replay_total` to `ReferenceController`

### Task 1: Add `replay_total` + `handle_replay_started` to `ReferenceController`

**Files:**
- Modify: `python/spinlab/capture/reference.py`
- Modify: `python/spinlab/session_manager.py`
- Modify: `python/spinlab/state_builder.py`
- Test: `tests/unit/capture/test_replay.py` (or wherever replay-reference tests live)

- [ ] **Step 1: Audit existing test homes for replay-controller tests**

```bash
grep -rln "handle_replay_finished\|handle_replay_error\|ReplayStartedEvent" tests/unit/
```

Pick the file with the most existing replay-controller tests as the home for the new test. If unclear, `tests/unit/capture/test_replay.py` is canonical.

- [ ] **Step 2: Write the failing test for `ReferenceController.handle_replay_started`**

In the chosen test file, add:

```python
def test_handle_replay_started_sets_replay_total():
    """ReferenceController.handle_replay_started records the frame count
    from the event onto replay_total. handle_replay_finished and
    handle_replay_error reset it back to 0."""
    from spinlab.capture.reference import ReferenceController
    from spinlab.db import Database
    from spinlab.protocol import ReplayStartedEvent
    from tests.conftest import FakeEmuBackend

    db = Database(":memory:")
    db.upsert_game("g1", "Test Game", "any%")
    emu = FakeEmuBackend(connected=False)
    ctl = ReferenceController(db, emu)

    assert ctl.replay_total == 0

    ctl.handle_replay_started(ReplayStartedEvent(path="x.replay", frame_count=2273))
    assert ctl.replay_total == 2273

    ctl.handle_replay_finished()
    assert ctl.replay_total == 0

    ctl.handle_replay_started(ReplayStartedEvent(path="x.replay", frame_count=500))
    ctl.handle_replay_error()
    assert ctl.replay_total == 0
```

- [ ] **Step 3: Run test to verify it fails**

```bash
python -m pytest tests/unit/capture/test_replay.py::test_handle_replay_started_sets_replay_total -v
```

Expected: `AttributeError: 'ReferenceController' object has no attribute 'replay_total'` (or similar).

- [ ] **Step 4: Add the field and method to `ReferenceController`**

In `python/spinlab/capture/reference.py`, find `ReferenceController.__init__` and add the `replay_total` field after the existing assignments (next to `paused_run_id` is a good neighbor). Replace:

```python
        # `paused_run_id` and `recorder.capture_run_id` form a mutually
        # exclusive pair: at most one is set at any time, encoding the run
        # phase (idle / recording / paused). Mutate ONLY through
        # `_enter_recording` / `_enter_paused` / `_enter_idle`, which assert
        # the invariant after each transition.
        self.paused_run_id: str | None = None
```

with:

```python
        # `paused_run_id` and `recorder.capture_run_id` form a mutually
        # exclusive pair: at most one is set at any time, encoding the run
        # phase (idle / recording / paused). Mutate ONLY through
        # `_enter_recording` / `_enter_paused` / `_enter_idle`, which assert
        # the invariant after each transition.
        self.paused_run_id: str | None = None

        # Total frame count of the currently-playing replay (set by
        # handle_replay_started from ReplayStartedEvent.frame_count, cleared
        # by handle_replay_finished / handle_replay_error). Surfaces to the
        # dashboard via StateBuilder as state["replay"]["total"]. Per-frame
        # progress is not observable under the RA backend — this total is
        # the only replay-progress signal we expose.
        self.replay_total: int = 0
```

Add the `handle_replay_started` method. Find the existing `handle_replay_finished` method and add the new method directly above it:

```python
    def handle_replay_started(self, event: ReplayStartedEvent) -> None:
        """Record the replay frame count from the started event."""
        self.replay_total = event.frame_count
```

Update `handle_replay_finished` to reset `replay_total`:

```python
    def handle_replay_finished(self) -> None:
        # End the session and leave the run paused — the user can finalize or discard.
        # recover_paused_capture_run excludes replay_ IDs, so this draft won't clobber
        # a real paused reference run on the next dashboard restart.
        self.replay_total = 0
        self._end_current_session(end_reason="stopped")
```

Update `handle_replay_error` to reset `replay_total`:

```python
    def handle_replay_error(self) -> None:
        self.replay_total = 0
        run_id = self.recorder.capture_run_id
        seg_count = self.db.count_segments_for_run(run_id) if run_id else 0
        self._end_current_session(end_reason="replay_error")
        if run_id and seg_count == 0:
            # Errored with nothing captured — discard the empty run.
            self.db.hard_delete_capture_run(run_id)
            self._enter_idle()
        # If segments were captured before the error, leave as paused so the user
        # can decide whether to finalize or discard them.
```

Add the import at the top of `reference.py` if not already present (it should be — verify with grep):

```bash
grep -n "ReplayStartedEvent" python/spinlab/capture/reference.py
```

If missing, add to the `from ..protocol import (...)` block:

```python
from ..protocol import (
    SPEED_UNCAPPED,
    CheckpointEvent,
    DeathEvent,
    LevelEntranceEvent,
    LevelExitEvent,
    ReferenceStartCmd,
    ReferenceStopCmd,
    ReplayCmd,
    ReplayStartedEvent,
    ReplayStopCmd,
    SpawnEvent,
)
```

- [ ] **Step 5: Run test to verify it passes**

```bash
python -m pytest tests/unit/capture/test_replay.py::test_handle_replay_started_sets_replay_total -v
```

Expected: PASS.

- [ ] **Step 6: Update `SessionManager` to delegate to capture**

In `python/spinlab/session_manager.py`:

(a) Delete the `replay_total` field block (around lines 91-96):

```python
        # Total frame count of the currently-playing replay, sourced from
        # ReplayStartedEvent.frame_count. Surfaces to the dashboard as
        # state["replay"]["total"]. Per-frame progress isn't observable
        # under the RA backend (no orchestrator hook for frame ticks), so
        # this is the only replay-progress signal we expose today.
        self.replay_total: int = 0
```

Just delete the whole block.

(b) Update `_handle_replay_started` (around line 326):

Replace:

```python
    async def _handle_replay_started(self, event: ReplayStartedEvent) -> None:
        self.replay_total = event.frame_count
        await self._notify_sse()
```

with:

```python
    async def _handle_replay_started(self, event: ReplayStartedEvent) -> None:
        self.capture.handle_replay_started(event)
        await self._notify_sse()
```

(c) Update `_handle_replay_finished` (around line 330): remove the `self.replay_total = 0` line. The `handle_replay_finished` on the controller now does it. Replace:

```python
    async def _handle_replay_finished(self, event: ReplayFinishedEvent) -> None:
        # handle_replay_finished ends the session and leaves the run paused if
        # segments were captured.  We must NOT call _clear_ref_and_idle here —
        # that would wipe paused_run_id and prevent the user from finalizing.
        self.replay_total = 0
        self.capture.handle_replay_finished()
        self.mode = Mode.IDLE
        await self._notify_sse()
```

with:

```python
    async def _handle_replay_finished(self, event: ReplayFinishedEvent) -> None:
        # handle_replay_finished ends the session and leaves the run paused if
        # segments were captured.  We must NOT call _clear_ref_and_idle here —
        # that would wipe paused_run_id and prevent the user from finalizing.
        self.capture.handle_replay_finished()
        self.mode = Mode.IDLE
        await self._notify_sse()
```

(d) Update `_handle_replay_error` (around line 339): remove the `self.replay_total = 0` line. Replace:

```python
    async def _handle_replay_error(self, event: ReplayErrorEvent) -> None:
        logger.warning("replay_error: %s", event.message)
        # Same as _handle_replay_finished: preserve paused_run_id set by
        # handle_replay_error when segments were captured before the error.
        self.replay_total = 0
        self.capture.handle_replay_error()
        self.mode = Mode.IDLE
        await self._notify_sse()
```

with:

```python
    async def _handle_replay_error(self, event: ReplayErrorEvent) -> None:
        logger.warning("replay_error: %s", event.message)
        # Same as _handle_replay_finished: preserve paused_run_id set by
        # handle_replay_error when segments were captured before the error.
        self.capture.handle_replay_error()
        self.mode = Mode.IDLE
        await self._notify_sse()
```

- [ ] **Step 7: Update `StateBuilder` to read from `capture`**

In `python/spinlab/state_builder.py`, find the replay block (around line 76):

```python
        if mode == Mode.REPLAY:
            base["replay"] = {
                "rec_path": session.capture.rec_path,
                "total": session.replay_total,
            }
```

Change to:

```python
        if mode == Mode.REPLAY:
            base["replay"] = {
                "rec_path": session.capture.rec_path,
                "total": session.capture.replay_total,
            }
```

- [ ] **Step 8: Run capture tests + state_builder tests + session_manager tests**

```bash
python -m pytest tests/unit/capture/ tests/unit/test_state_builder.py tests/unit/test_session_manager.py -v
```

Expected: PASS.

- [ ] **Step 9: Run the full fast suite**

```bash
python -m pytest -m "not emulator"
```

Expected: PASS.

- [ ] **Step 10: pyright check on the touched modules**

```bash
npx pyright python/spinlab/capture/reference.py python/spinlab/session_manager.py python/spinlab/state_builder.py
```

Expected: 0 errors / 0 warnings on the changes (any pre-existing errors are tracked debt).

- [ ] **Step 11: Commit**

```bash
git add python/spinlab/capture/reference.py python/spinlab/session_manager.py python/spinlab/state_builder.py tests/unit/capture/test_replay.py
git commit -m "capture: move replay_total from SessionManager to ReferenceController"
```

---

## Phase 2: C3 — Fix the eager mode-flip race

### Task 2: Gate capture-routing handlers on `capture.is_recording`

**Files:**
- Modify: `python/spinlab/session_manager.py` — 5 handler methods
- Test: `tests/unit/test_session_manager.py` — regression test

The 5 handlers that currently gate on `if self.mode in (Mode.REFERENCE, Mode.REPLAY)` route their events to `self.capture`. That's the wrong gate — the authoritative bit for "capture should consume this event" is `capture.is_recording`. Switching the gate eliminates the race condition that the eager mode flip in `start_replay` was working around.

- [ ] **Step 1: Write the failing regression test**

In `tests/unit/test_session_manager.py`, add this test. (Locate an appropriate test class or place at module scope; match the file's existing patterns.)

```python
async def test_capture_event_routed_while_recording_even_if_mode_lags(db, mock_emu, tmp_path):
    """The race C3 fixes: between capture.start_replay setting is_recording=True
    and the SessionManager's mode flipping to REPLAY, the poller can emit a
    LevelEntranceEvent. The handler must route it to capture based on the
    recording flag, not the mode."""
    from spinlab.models import Mode
    from spinlab.protocol import LevelEntranceEvent
    from spinlab.session_manager import SessionManager

    sm = SessionManager(
        db=db, emu=mock_emu, rom_dir=None,
        default_category="any%", data_dir=tmp_path,
    )
    sm.game_id = "g1"
    sm.game_name = "Test Game"
    db.upsert_game("g1", "Test Game", "any%")

    # Simulate the in-flight state: capture has been told to start replay
    # (recorder is armed with a run id), but mode hasn't flipped yet.
    sm.capture._enter_recording("replay_test123", "sess_test123")
    sm.mode = Mode.IDLE  # mode lags

    assert sm.capture.is_recording

    # Spy on the handler.
    handle_entrance_calls = []
    async def _spy(ev):
        handle_entrance_calls.append(ev)
    sm.capture.handle_entrance = _spy  # type: ignore[method-assign]

    await sm.route_event(LevelEntranceEvent(level=1, room=0))

    assert len(handle_entrance_calls) == 1, (
        "LevelEntranceEvent must be routed to capture while is_recording=True, "
        "regardless of mode"
    )
```

The fixtures `db` and `mock_emu` are defined in `tests/conftest.py`. Use the import style already in `test_session_manager.py`.

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/unit/test_session_manager.py::test_capture_event_routed_while_recording_even_if_mode_lags -v
```

Expected: FAIL — `assert len(handle_entrance_calls) == 1` fails because the current handler returns early when `mode == IDLE`.

- [ ] **Step 3: Update `_handle_level_entrance`**

In `python/spinlab/session_manager.py`, find:

```python
    async def _handle_level_entrance(self, event: LevelEntranceEvent) -> None:
        if self.mode not in (Mode.REFERENCE, Mode.REPLAY):
            return
        await self.capture.handle_entrance(event)
        await self._notify_sse()
```

Replace with:

```python
    async def _handle_level_entrance(self, event: LevelEntranceEvent) -> None:
        if not self.capture.is_recording:
            return
        await self.capture.handle_entrance(event)
        await self._notify_sse()
```

- [ ] **Step 4: Update `_handle_checkpoint`**

Find:

```python
    async def _handle_checkpoint(self, event: CheckpointEvent) -> None:
        if self.mode not in (Mode.REFERENCE, Mode.REPLAY):
            return
        await self.capture.handle_checkpoint(event, self.require_game())
        await self._notify_sse()
```

Replace with:

```python
    async def _handle_checkpoint(self, event: CheckpointEvent) -> None:
        if not self.capture.is_recording:
            return
        await self.capture.handle_checkpoint(event, self.require_game())
        await self._notify_sse()
```

- [ ] **Step 5: Update `_handle_death`**

The Death handler also routes to `self.capture` when `mode in (REFERENCE, REPLAY)`. Find:

```python
    async def _handle_death(self, event: DeathEvent) -> None:
        if self.mode == Mode.COLD_FILL:
            logger.info("death during cold_fill — waiting for respawn")
            return
        if self.mode in (Mode.REFERENCE, Mode.REPLAY):
            self.capture.handle_death(event)
            return
        if self.mode == Mode.PRACTICE and self.practice_session:
            await self.practice_session.handle_death()
```

Replace with:

```python
    async def _handle_death(self, event: DeathEvent) -> None:
        if self.mode == Mode.COLD_FILL:
            logger.info("death during cold_fill — waiting for respawn")
            return
        if self.capture.is_recording:
            self.capture.handle_death(event)
            return
        if self.mode == Mode.PRACTICE and self.practice_session:
            await self.practice_session.handle_death()
```

- [ ] **Step 6: Update `_handle_spawn`**

This one is subtler — it has multiple branches (COLD_FILL, FILL_GAP, then REFERENCE/REPLAY). Only the last branch changes. Find:

```python
    async def _handle_spawn(self, event: SpawnEvent) -> None:
        if self.mode == Mode.COLD_FILL:
            done = await self.cold_fill.handle_spawn(event)
            if done:
                self.mode = Mode.IDLE
                # Power-cycle the emulator so the user lands at the title
                # screen instead of mid-respawn in whatever level the last
                # capture happened in.
                try:
                    await self.emu.send_command(ResetCmd())
                except (ConnectionError, OSError):
                    logger.warning("cold_fill: reset command failed (backend gone)")
            await self._notify_sse()
            return
        if self.mode == Mode.FILL_GAP:
            if self.fill_gap.handle_spawn(event):
                self.mode = Mode.IDLE
                await self._notify_sse()
            return
        if self.mode in (Mode.REFERENCE, Mode.REPLAY):
            self.capture.handle_spawn(event, self.require_game())
```

Change only the last branch:

```python
    async def _handle_spawn(self, event: SpawnEvent) -> None:
        if self.mode == Mode.COLD_FILL:
            done = await self.cold_fill.handle_spawn(event)
            if done:
                self.mode = Mode.IDLE
                # Power-cycle the emulator so the user lands at the title
                # screen instead of mid-respawn in whatever level the last
                # capture happened in.
                try:
                    await self.emu.send_command(ResetCmd())
                except (ConnectionError, OSError):
                    logger.warning("cold_fill: reset command failed (backend gone)")
            await self._notify_sse()
            return
        if self.mode == Mode.FILL_GAP:
            if self.fill_gap.handle_spawn(event):
                self.mode = Mode.IDLE
                await self._notify_sse()
            return
        if self.capture.is_recording:
            self.capture.handle_spawn(event, self.require_game())
```

- [ ] **Step 7: Update `_handle_level_exit`**

Find:

```python
    async def _handle_level_exit(self, event: LevelExitEvent) -> None:
        if self.mode == Mode.PRACTICE and self.practice_session and event.goal == "abort":
            # Pit-fall / death-fall — same reload semantics as a Death event.
            await self.practice_session.handle_level_exit_abort()
            return
        if self.mode not in (Mode.REFERENCE, Mode.REPLAY):
            return
        self.capture.handle_exit(event, self.require_game())
        await self._notify_sse()
```

Replace with:

```python
    async def _handle_level_exit(self, event: LevelExitEvent) -> None:
        if self.mode == Mode.PRACTICE and self.practice_session and event.goal == "abort":
            # Pit-fall / death-fall — same reload semantics as a Death event.
            await self.practice_session.handle_level_exit_abort()
            return
        if not self.capture.is_recording:
            return
        self.capture.handle_exit(event, self.require_game())
        await self._notify_sse()
```

- [ ] **Step 8: Run the regression test to verify it now passes**

```bash
python -m pytest tests/unit/test_session_manager.py::test_capture_event_routed_while_recording_even_if_mode_lags -v
```

Expected: PASS.

- [ ] **Step 9: Run the full fast suite**

```bash
python -m pytest -m "not emulator"
```

Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add python/spinlab/session_manager.py tests/unit/test_session_manager.py
git commit -m "session_manager: gate capture handlers on is_recording, not mode"
```

---

### Task 3: Remove the eager mode flip in `start_replay`

**Files:**
- Modify: `python/spinlab/session_manager.py:379-412`

With Task 2's gate change, the eager mode flip is no longer functionally required: the recorder is armed (`is_recording=True`) before `ReplayCmd` is sent, so any event observed by the poller during the await window routes to capture without needing mode to be REPLAY yet.

Collapse `start_replay` back to the simple shape used by `start_reference`, `start_practice`, etc.

- [ ] **Step 1: Replace `start_replay`**

In `python/spinlab/session_manager.py`, find:

```python
    async def start_replay(self, spinrec_path: str, speed: int = SPEED_UNCAPPED) -> ActionResult:
        # Flip mode to REPLAY eagerly, BEFORE capture.start_replay sends
        # ReplayCmd through the orchestrator. capture.start_replay's
        # pre-flight checks still see the prior mode (passed as the first
        # argument), so AlreadyReplayingError / etc. logic is unaffected.
        #
        # Why: once ReplayCmd is sent, the orchestrator's _on_replay fires
        # PLAY_REPLAY and the poller may observe the level-entrance edge
        # (level_start 0→1) within a few frames — well before
        # ReplayStartedEvent has propagated back through the event queue
        # to flip mode here. If mode is still IDLE at that point, the
        # level-entrance event is dropped by _handle_level_entrance's
        # "if self.mode not in (REFERENCE, REPLAY): return" gate, and
        # no segments form. Setting mode eagerly closes the race.
        #
        # On preflight failure (DraftPendingError, AlreadyReplayingError,
        # NotConnectedError, etc.) the exception propagates and the mode
        # rollback in the except branch restores the prior state.
        prev_mode = self.mode
        self.mode = Mode.REPLAY
        try:
            result = await self.capture.start_replay(
                prev_mode, self.require_game(), spinrec_path, speed,
            )
        except Exception:
            self.mode = prev_mode
            raise
        if result.new_mode is not None and result.new_mode != Mode.REPLAY:
            # Defensive: if capture decides this isn't actually a replay
            # transition (shouldn't happen in current flow), honor the
            # returned mode rather than leaving the eager flip in place.
            self.mode = result.new_mode
        await self._notify_sse()
        return result
```

Replace with:

```python
    async def start_replay(self, spinrec_path: str, speed: int = SPEED_UNCAPPED) -> ActionResult:
        # capture.start_replay arms the recorder (is_recording=True) BEFORE
        # sending ReplayCmd, so any LevelEntrance the poller observes during
        # the send_command await reaches capture via the is_recording gate
        # in _handle_level_entrance — no eager mode flip needed.
        return await self._apply_result(
            await self.capture.start_replay(
                self.mode, self.require_game(), spinrec_path, speed,
            )
        )
```

- [ ] **Step 2: Run the regression test from Task 2 + the existing replay tests**

```bash
python -m pytest tests/unit/test_session_manager.py -k "replay or recording" -v
```

Expected: PASS. The race regression test from Task 2 still passes; existing replay tests still pass.

- [ ] **Step 3: Run the full fast suite**

```bash
python -m pytest -m "not emulator"
```

Expected: PASS.

- [ ] **Step 4: Run the integration replay test if RA is available**

```bash
python -m pytest tests/integration/test_replay_fixture.py -v
```

Expected: PASS, OR skip if RetroArch is not running (the fixture marks emulator-only). If skipped, note that in the report.

- [ ] **Step 5: pyright on the touched file**

```bash
npx pyright python/spinlab/session_manager.py
```

Expected: 0 errors (or no new errors versus the baseline).

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/session_manager.py
git commit -m "session_manager: collapse start_replay's eager mode-flip workaround"
```

---

## Phase 3: Final verification

### Task 4: Full suite + memory update

- [ ] **Step 1: Run the full pytest suite**

```bash
python -m pytest
```

Expected: PASS — same test count as the Bundle 2 baseline (~873) plus 2 new tests (Task 1 and Task 2). Expected total: ~875.

- [ ] **Step 2: Confirm `SessionManager.replay_total` is fully gone**

```bash
grep -rn "replay_total" python/
```

Expected: matches only in `python/spinlab/capture/reference.py` (the new home) and `python/spinlab/state_builder.py` (reads it via `session.capture.replay_total`). Zero matches in `session_manager.py`.

- [ ] **Step 3: Confirm the eager-flip workaround is fully gone**

```bash
grep -n "Flip mode to REPLAY eagerly\|prev_mode = self.mode\|self.mode = prev_mode" python/spinlab/session_manager.py
```

Expected: zero matches.

- [ ] **Step 4: Update memory**

Add a new project memory note (or extend `project_bundle2_ra_layer_cleanup_2026_05_12.md` with a sibling file for Bundle 3) summarizing the completed work: what moved, the race fix, the verification results. Update `MEMORY.md` index.

---

## Out of Scope

- Other Tier B / C candidates from the original audit (B1: consolidate finalize paths, B2: scheduler init branches, B3: ConditionRegistry split, B4: Kalman dedup, C4: estimator registration, A3: state pattern).

---

## Self-Review

**Spec coverage:**
- C1 (move `replay_total`): Task 1 covers field add + `handle_replay_started` method + three callsite updates (SessionManager → capture, StateBuilder → capture.replay_total).
- C3 (race fix): Tasks 2 and 3 cover the gate change (5 handlers) and the removal of the eager mode flip.

**Placeholder scan:** No TBDs, no "implement appropriate handling", every step has the actual code to paste. The "audit existing test homes" Step 1 of Task 1 is an enumeration via grep, not a placeholder.

**Type consistency:**
- `replay_total: int` on `ReferenceController`, used in Task 1 (definition) and StateBuilder (read).
- `handle_replay_started(event: ReplayStartedEvent)` on `ReferenceController`, used in Task 1 (definition) and SessionManager (delegation).
- `capture.is_recording: bool` (already exists on ReferenceController) — used as the gate in Task 2.
- `_apply_result(result)` helper (already exists on SessionManager) — used in Task 3.

Names match between definition and use.
