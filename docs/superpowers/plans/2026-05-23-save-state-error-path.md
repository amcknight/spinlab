# Save-State Error Path Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When `emu.save_state(seg_id)` raises during a reference-run entrance or checkpoint, stop writing a `waypoint_save_states` row pointing at a file that doesn't exist on disk. Today the exception is logged and the recorder keeps going — practice later tries to load the missing file and crashes cryptically.

**Architecture:** The bug lives in `capture/reference.py:423-450`. `handle_entrance` and `handle_checkpoint` call `await self.emu.save_state(seg_id)` in a try/except that only logs, then unconditionally pass the original event to `self.recorder.handle_entrance/handle_checkpoint`. The recorder buffers `event.state_path` and later writes it into `waypoint_save_states` at `recorder.py:189-196`. Fix: on save_state failure, pass a copy of the event with `state_path=None` to the recorder (it already has an `if state_path:` gate on the persisted row). A small ancillary observability fix in `capture/cold_fill.py:98-102` adds a structured "first save_state attempt failed, expecting retry on next spawn" log so the cold-fill retry path leaves a trail for debugging (F12).

**Tech Stack:** Python 3.11+, `dataclasses.replace` for frozen event copies, pytest + pytest-asyncio. No new dependencies.

**Scope reference:** `docs/superpowers/scans/2026-05-23-improve.md` → "high-leverage → CF-8 — Save-state error path → durable failure record". Absorbs F5 (real consistency bug) and F12 (observability).

---

### Task 1: Failing regression test — save_state exception leaves no waypoint_save_states row

**Goal:** Pin the new behavior with a unit test that fails today and passes after Task 2. Use the existing `FakeEmuBackend` from `tests/conftest.py` (it already supports `save_state` as a stub).

**Files:**
- Read: `tests/conftest.py` (find `FakeEmuBackend`)
- Read: `tests/unit/capture/test_reference.py` (find existing handle_entrance/handle_checkpoint test pattern, ~line 410-442)
- Modify: `tests/unit/capture/test_reference.py` (append the new test cases)

- [ ] **Step 1: Read the existing test file to learn the local fixture pattern**

Run: `python -m pytest tests/unit/capture/test_reference.py --collect-only -q | head -20`

Look at how `controller` and `fake_emu` fixtures are wired. Then look at `tests/unit/capture/test_reference.py:410-442` for the pattern of starting recording + dispatching one event.

- [ ] **Step 2: Add the FakeEmuBackend hook for raising on save_state**

If `FakeEmuBackend` in `tests/conftest.py` doesn't already let a test request "save_state raises", add a small attribute. Read `tests/conftest.py` first; the fake almost certainly has a `save_state_calls: list` already and may already have an `_raise_on_save_state` flag. If not, add:

```python
class FakeEmuBackend:
    def __init__(self, connected: bool = True) -> None:
        # ... existing fields ...
        self.save_state_should_raise: bool = False

    async def save_state(self, seg_id: str) -> None:
        # existing call log
        self.save_state_calls.append(seg_id)
        if self.save_state_should_raise:
            raise RuntimeError("simulated save_state failure")
```

(Only edit if the fake doesn't already support this. Search the file first — there is sometimes a `save_state_error` already.)

- [ ] **Step 3: Write the failing regression tests**

Append to `tests/unit/capture/test_reference.py`:

```python
class TestSaveStateFailureDoesNotPersistWaypointRow:
    """When save_state raises, the recorder must not buffer a dangling
    state_path. Otherwise, _close_segment writes a waypoint_save_states row
    pointing at a file that never landed on disk, and practice crashes on
    load. See docs/superpowers/scans/2026-05-23-improve.md → CF-8."""

    async def test_entrance_save_state_failure_strips_state_path(
        self, controller, fake_emu,
    ):
        from spinlab.protocol import LevelEntranceEvent

        controller._enter_recording("run_x", "sess_x")
        fake_emu.save_state_should_raise = True

        evt = LevelEntranceEvent(
            level=5, room=2, state_path="/tmp/should-not-be-persisted.state",
        )
        await controller.handle_entrance(evt)

        # save_state was still attempted (so we have a log trail), but the
        # recorder's pending_start now carries state_path=None.
        assert controller.recorder.pending_start is not None
        assert controller.recorder.pending_start.state_path is None

    async def test_checkpoint_save_state_failure_strips_state_path(
        self, controller, fake_emu,
    ):
        from spinlab.protocol import CheckpointEvent, LevelEntranceEvent

        controller._enter_recording("run_x", "sess_x")
        # Prime with an entrance so the checkpoint has a pending_start to close.
        await controller.handle_entrance(LevelEntranceEvent(level=5, room=2))

        fake_emu.save_state_should_raise = True
        evt = CheckpointEvent(
            level_num=5, cp_ordinal=1, state_path="/tmp/should-not-be-persisted.state",
        )
        await controller.handle_checkpoint(evt, "g1")

        # The checkpoint became the new pending_start (cp1 → next segment's
        # start). Its state_path must be None because save_state failed.
        assert controller.recorder.pending_start is not None
        assert controller.recorder.pending_start.state_path is None
```

- [ ] **Step 4: Run the tests to verify they fail**

Run: `python -m pytest tests/unit/capture/test_reference.py::TestSaveStateFailureDoesNotPersistWaypointRow -v`
Expected: FAIL — `pending_start.state_path` equals `"/tmp/should-not-be-persisted.state"` (the original event's path), not `None`.

- [ ] **Step 5: Commit the failing tests**

```bash
git add tests/conftest.py tests/unit/capture/test_reference.py
git commit -m "tests(capture): pin desired behavior — save_state failure strips state_path"
```

(Failing-test commit is fine on a feature branch and aids bisection if the fix later regresses. Per the project's Red-Green TDD note in CLAUDE.md.)

---

### Task 2: Strip state_path on save_state failure

**Goal:** Make the failing tests pass. In `capture/reference.py:423-450`, after a save_state exception, create a copy of the event with `state_path=None` and pass that to the recorder.

**Files:**
- Modify: `python/spinlab/capture/reference.py:423-450`

- [ ] **Step 1: Edit `handle_entrance`**

In `python/spinlab/capture/reference.py`, replace the existing `handle_entrance` body with:

```python
    async def handle_entrance(self, event: LevelEntranceEvent) -> None:
        logger.info("capture: entrance level=%s", event.level)
        if self.is_recording:
            from spinlab.state_paths import segment_id_for_event
            seg_id = segment_id_for_event(event)
            if seg_id:
                try:
                    await self.emu.save_state(seg_id)
                except Exception:
                    # Don't persist a waypoint row pointing at a file that
                    # didn't land on disk — practice would crash on load.
                    # The structured log surfaces the failure; downstream
                    # consumers see no state_path for this entrance.
                    logger.exception(
                        "save_state failed for entrance event seg_id=%r; "
                        "stripping state_path from recorded event",
                        seg_id,
                    )
                    from dataclasses import replace
                    event = replace(event, state_path=None)
        self.recorder.handle_entrance(event)
```

- [ ] **Step 2: Edit `handle_checkpoint`** (same pattern)

Replace the `handle_checkpoint` body with:

```python
    async def handle_checkpoint(self, event: CheckpointEvent, game_id: str) -> None:
        logger.info("capture: checkpoint level=%s cp=%s",
                     event.level_num, event.cp_ordinal)
        if self.is_recording:
            from spinlab.state_paths import segment_id_for_event
            seg_id = segment_id_for_event(event)
            if seg_id:
                try:
                    await self.emu.save_state(seg_id)
                except Exception:
                    logger.exception(
                        "save_state failed for checkpoint event seg_id=%r; "
                        "stripping state_path from recorded event",
                        seg_id,
                    )
                    from dataclasses import replace
                    event = replace(event, state_path=None)
        self.recorder.handle_checkpoint(event, game_id)
```

(Optional micro-cleanup: hoist `from dataclasses import replace` to the top of the file alongside the other imports. Equivalent behavior; just tidier.)

- [ ] **Step 3: Run the regression tests**

Run: `python -m pytest tests/unit/capture/test_reference.py::TestSaveStateFailureDoesNotPersistWaypointRow -v`
Expected: PASS.

- [ ] **Step 4: Run the broader reference test suite**

Run: `python -m pytest tests/unit/capture/ -v`
Expected: PASS. (No other test should break — we only changed the failure path, and the success path is identical.)

- [ ] **Step 5: Run the fast suite**

Run: `python -m pytest -m "not emulator" -q`
Expected: green.

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/capture/reference.py
git commit -m "capture: strip state_path on save_state failure (don't persist dangling waypoint rows)"
```

---

### Task 3: Structured first-failure log in cold_fill

**Goal:** Cold-fill's `handle_spawn` already aborts the segment on save_state failure (it does NOT advance the queue and does NOT write a DB row — that path is correct). The remaining gap is observability: when the user retries by dying again, the second attempt has no log trail tying it to the first failure. F12 from the scan.

Add a structured-log breadcrumb so the next reader can see "this segment failed once before this run succeeded" (or "this segment has failed N times").

**Files:**
- Modify: `python/spinlab/capture/cold_fill.py:87-119`

- [ ] **Step 1: Read the current handle_spawn**

Open `python/spinlab/capture/cold_fill.py`. The existing block at lines 87-119 has a single `log.warn(...)` on save_state failure. The fix is small: include a counter on the controller so successive failures count up, and the log message names the count.

- [ ] **Step 2: Add a per-segment retry counter**

In `ColdFillController.__init__` (read the actual file first to find the right line; the current state initialization block holds `self.queue`, `self.current`, etc.), add:

```python
        # Per-segment retry counter for save_state failures. Keyed by
        # segment_id (the cold-fill segment id from `self.current`). Lets
        # the next spawn after a failure log "attempt 2/N", surfacing
        # repeated failures that would otherwise drown in single-line
        # warnings.
        self._save_state_attempts: dict[str, int] = {}
```

Make sure to reset it in `clear()` (which currently zeroes `queue`/`current`/`total`):

```python
    def clear(self) -> None:
        """Reset cold-fill state (e.g., on disconnect)."""
        self.queue = []
        self.current = None
        self.total = 0
        self._save_state_attempts = {}
```

- [ ] **Step 3: Use the counter in handle_spawn**

Replace the current save_state try/except block (around lines 96-103) with:

```python
        seg_id = event.segment_id or self.current
        attempt = self._save_state_attempts.get(seg_id, 0) + 1
        self._save_state_attempts[seg_id] = attempt
        try:
            await self.emu.save_state(seg_id)
        except Exception as exc:
            log.warn(
                logger,
                "cold_fill: save_state failed; segment will be retried on next spawn",
                exc=exc, segment_id=seg_id, attempt=attempt,
            )
            return False
        # Success — clear the retry counter for this segment so future
        # cold-fill passes don't carry the count forward.
        self._save_state_attempts.pop(seg_id, None)
```

- [ ] **Step 4: Add a test for the retry-count log**

Append to `tests/unit/capture/test_cold_fill.py`:

```python
class TestColdFillSaveStateRetryCount:
    """First save_state failure should log attempt=1; second should log attempt=2.
    The success path on attempt 3 clears the counter."""

    async def test_repeated_failures_increment_attempt_count(
        self, emu, cold_fill_db, caplog,
    ):
        import logging
        from spinlab.capture.cold_fill import ColdFillController
        from spinlab.protocol import SpawnEvent

        cc = ColdFillController(cold_fill_db, emu)
        await cc.start("g")

        emu.save_state_should_raise = True
        with caplog.at_level(logging.WARNING, logger="spinlab.capture.cold_fill"):
            await cc.handle_spawn(SpawnEvent(state_path="/cold1.mss"))
            await cc.handle_spawn(SpawnEvent(state_path="/cold1.mss"))

        # Confirm the WARNING records carry attempt=1 then attempt=2.
        # Allow `log.warn` to use either an `attempt=` key in the message
        # or structured `extra` kwargs — the structured-log helper varies
        # across the codebase. Read whichever form is in the recorded
        # message string.
        msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any("attempt=1" in m for m in msgs), f"missing attempt=1 log; got {msgs}"
        assert any("attempt=2" in m for m in msgs), f"missing attempt=2 log; got {msgs}"
```

(If the project's `log.warn` helper formats `attempt=1` as `attempt: 1` or similar, adjust the substring match. Check the helper's signature in `python/spinlab/log.py` before writing the assertion — and pin to whatever format it actually emits. If unclear, run the test once to read the actual log output and tighten the assertion.)

- [ ] **Step 5: Run the new test**

Run: `python -m pytest tests/unit/capture/test_cold_fill.py::TestColdFillSaveStateRetryCount -v`
Expected: PASS (after Step 3 has been applied).

- [ ] **Step 6: Run the broader cold_fill test suite**

Run: `python -m pytest tests/unit/capture/test_cold_fill.py -v`
Expected: PASS — no other cold-fill test depends on the retry-counter dict, but verify before committing.

- [ ] **Step 7: Commit**

```bash
git add python/spinlab/capture/cold_fill.py tests/unit/capture/test_cold_fill.py
git commit -m "capture(cold_fill): log save_state retry attempt count for failed segments"
```

---

### Task 4: Full-suite verification

**Goal:** Per the project's CLAUDE.md "Merging Branches" rule: full suite must pass before merging. Verify both fast and emulator tracks.

**Files:** none modified

- [ ] **Step 1: Run the full unfiltered suite**

Run: `python -m pytest -q`

Expected: green. If anything fails:
- If the failure is in `tests/unit/capture/` — likely a real regression from Tasks 2 or 3. Investigate.
- If the failure is in emulator tests with `proc.poll()=3221225477` in the diagnostic block — Mode 3 RA crash, pre-existing flake per `project_test_reliability_known_issues.md`. Surface to user before declaring done; the rule from CLAUDE.md `feedback_red_baseline_habit` is "never silently move on."

- [ ] **Step 2: If green, mark the plan complete**

```bash
git log --oneline main..HEAD
```

Confirm all expected commits landed.

---

## Self-Review

**1. Spec coverage:**
- ✅ F5 (`capture/reference.py:431-434` — save_state failure → DB inconsistency) — Tasks 1 + 2.
- ✅ F12 (`capture/cold_fill.py:98-102` — first-failure trace lost) — Task 3.
- ⚠️ B6 (`capture/cold_fill.py:87-119` — "synchronous" `await save_state` in handler) — SKEPTIC verified this as misuse of "synchronous" (it's already async). Not in scope of this plan; the convergence summary in the scan already notes B6 is "marginal" — drop without action.
- ✅ Full-suite verification — Task 4.

**2. Placeholder scan:**
- ✅ Every code step shows the full code.
- ✅ Every test step shows the full test body.
- ✅ Step 2 of Task 3 includes the actual __init__ initialization line and clear() update.
- ✅ Optional caveats are flagged inline (e.g., "If `log.warn` formats differently…") — these are runtime-discoverable details, not work-deferrals.

**3. Type consistency:**
- ✅ `dataclasses.replace(event, state_path=None)` returns the same dataclass type (`LevelEntranceEvent` / `CheckpointEvent`) — both have `state_path: str | None = None` per `protocol.py:43, :52`, so `None` is a valid value.
- ✅ `_save_state_attempts: dict[str, int]` matches the access patterns in `handle_spawn` (string key, int value) and `clear()` (reassigned to `{}`).
- ✅ `seg_id` is `str` in both `handle_spawn` and the new dict, since `self.current` is `str | None` and the early-return at line 89 guarantees it's `str` by the time we index.

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-05-23-save-state-error-path.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

**Which approach?**
