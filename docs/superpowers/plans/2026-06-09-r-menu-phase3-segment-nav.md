# R-menu Phase 3 — Segment History Navigation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add **R+→ (next segment)** and **R+← (previous segment)** — browser-style navigation through the segments practiced this session — driven by a visit-history stack + cursor on `PracticeSession`.

**Architecture:** `PracticeSession` gains `_history` (ordered `_HistoryEntry` records) + `_cursor`. `run_one` loads the segment **at the cursor** (a fresh scheduler pick appended when the cursor is past the end), waits for a result OR a nav signal, and on a real completion advances the cursor forward by one. Nav commands move the cursor and **drop the in-flight attempt** (reusing the pause disarm path — nothing recorded), then wake `run_one` so it loads the cursor's segment. The nav signal reuses the existing `_result_event` wakeup (a `_nav_pending` flag distinguishes it from a real result), so nav is as snappy as a result and pause is untouched.

**Tech Stack:** Python 3.11 (async practice loop), pytest. Builds on Phases 1–2 (`c39f590`).

**Spec:** `docs/superpowers/specs/2026-06-09-r-menu-vocabulary-expansion-design.md` (Phase 3).

## Decisions locked before writing

1. **Completion walks forward one** (Andrew's call): a completed attempt advances `cursor += 1` uniformly — at the end that's a fresh pick; mid-history it re-practices forward toward the present. Simple and uniform.
2. **Nav drops the attempt, no clock freeze.** Unlike pause (which freezes the session clock), nav abandons the current attempt and moves on — the time spent counts. It reuses the `PracticePauseCmd` disarm to drop the backend timing, but does **not** touch `pause_offset_sec`.
3. **History stores `_HistoryEntry(load_cmd, allocator)`** — everything `run_one` needs to reload + record a revisit without re-consulting the scheduler. The original pick's allocator rides along (a revisit's `chosen_allocator` reflects why the segment first entered the queue).
4. **Stale revisit targets skip forward.** If a history entry's state file has vanished (segment invalidated/deleted since first visit), `run_one` logs once and steps the cursor forward — fresh scheduler picks are always valid (the scheduler asserts `state_path`).
5. **Nav is gated to "armed, not paused."** `go_prev`/`skip_next` no-op unless an attempt is in flight (`_current_state_path is not None`) and not paused. `go_prev` at cursor 0 is a no-op.
6. **D-pad bits:** Right = `$15` `0x01`, Left = `$15` `0x02` (kaizosplits buttonsHeld1: `... Up Down Left Right`). Registered as `(HELD1, ...) -> next_segment / prev_segment`.

## File map

| File | Change |
|---|---|
| `python/spinlab/retroarch/menu_detector.py` | + `BUTTON_LEFT`/`BUTTON_RIGHT`; register prev/next_segment |
| `tests/unit/retroarch/test_menu_detector.py` | Left/Right dispatch tests |
| `python/spinlab/practice.py` | history + cursor + `go_prev`/`skip_next` + `run_one` restructure |
| `tests/unit/test_practice.py` | history/cursor transition + nav tests |
| `python/spinlab/session_manager.py` | dispatch prev/next_segment |
| `tests/unit/test_session_manager.py` | nav dispatch tests |
| `tests/integration/scenarios/menu_nav.poke` | **new** d-pad scenario |
| `tests/integration/test_transitions.py` | emulator test for a d-pad command |

---

### Task 1: Register prev/next_segment (R+←/→) in the detector

**Files:**
- Modify: `python/spinlab/retroarch/menu_detector.py`
- Test: `tests/unit/retroarch/test_menu_detector.py`

- [ ] **Step 1: Write the failing tests**

In `tests/unit/retroarch/test_menu_detector.py`, add `BUTTON_LEFT, BUTTON_RIGHT` to the `from spinlab.retroarch.menu_detector import (...)` block and append:

```python
def test_right_after_r_dispatches_next_segment():
    d = ControllerMenuDetector()
    events = _run(d, [
        _snap(controller_held=BUTTON_R),
        _snap(controller_held=BUTTON_R, controller_held_1=BUTTON_RIGHT),
    ])
    cmds = _cmds(events)
    assert len(cmds) == 1 and cmds[0].command == "next_segment"


def test_left_after_r_dispatches_prev_segment():
    d = ControllerMenuDetector()
    events = _run(d, [
        _snap(controller_held=BUTTON_R),
        _snap(controller_held=BUTTON_R, controller_held_1=BUTTON_LEFT),
    ])
    cmds = _cmds(events)
    assert len(cmds) == 1 and cmds[0].command == "prev_segment"
```

- [ ] **Step 2: Run — verify fail**

Run: `python -m pytest tests/unit/retroarch/test_menu_detector.py -q`
Expected: FAIL — `BUTTON_LEFT`/`BUTTON_RIGHT` import error.

- [ ] **Step 3: Add constants + registry entries**

In `python/spinlab/retroarch/menu_detector.py`, after the `BUTTON_Y` line add:

```python
BUTTON_LEFT = 0x02   # $15 — previous segment
BUTTON_RIGHT = 0x01  # $15 — next segment
```

Extend `COMMANDS`:

```python
COMMANDS: dict[ButtonKey, str] = {
    (HELD2, BUTTON_X): "pause",
    (HELD1, BUTTON_Y): "toggle_practice",
    (HELD1, BUTTON_RIGHT): "next_segment",
    (HELD1, BUTTON_LEFT): "prev_segment",
}
```

Update the docstring command list to: "X = pause, Y = toggle_practice, and ←/→ = prev/next segment are the commands today."

- [ ] **Step 4: Run — verify pass**

Run: `python -m pytest tests/unit/retroarch/test_menu_detector.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/retroarch/menu_detector.py tests/unit/retroarch/test_menu_detector.py
git commit -m "feat(menu): register R+left/right = prev/next_segment"
```

---

### Task 2: PracticeSession history + cursor + nav

**Files:**
- Modify: `python/spinlab/practice.py`
- Test: `tests/unit/test_practice.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_practice.py`:

```python
class TestSegmentNavigation:
    def _session(self, practice_db):
        from unittest.mock import AsyncMock
        emu = AsyncMock(); emu.is_connected = True; emu.send_command = AsyncMock()
        ps = PracticeSession(emu=emu, db=practice_db, game_id="g",
                             scheduler=Scheduler(practice_db, "g"))
        ps.is_running = True
        return ps

    def test_segment_at_cursor_picks_and_appends_at_end(self, practice_db):
        ps = self._session(practice_db)
        # Fresh session: cursor 0, empty history -> a pick is appended.
        entry = ps._segment_at_cursor()
        assert entry is not None
        assert len(ps._history) == 1
        assert ps._cursor == 0
        assert entry.load_cmd.id == ps._history[0].load_cmd.id

    def test_completion_advances_cursor(self, practice_db):
        ps = self._session(practice_db)
        ps._segment_at_cursor()           # cursor 0, history len 1
        ps._advance_after_completion()    # cursor -> 1
        assert ps._cursor == 1
        ps._segment_at_cursor()           # cursor 1 >= len 1 -> pick + append
        assert len(ps._history) == 2 and ps._cursor == 1

    @pytest.mark.asyncio
    async def test_go_prev_moves_cursor_back_and_drops_attempt(self, practice_db):
        from spinlab.protocol import PracticePauseCmd
        ps = self._session(practice_db)
        # Two segments visited; cursor at the second.
        ps._segment_at_cursor(); ps._advance_after_completion(); ps._segment_at_cursor()
        assert ps._cursor == 1
        ps._current_state_path = "s.state"   # simulate an armed attempt
        await ps.go_prev()
        assert ps._cursor == 0
        assert ps._nav_pending is True
        sent = [c.args[0] for c in ps.emu.send_command.call_args_list]
        assert any(isinstance(c, PracticePauseCmd) for c in sent)
        assert ps._result_event.is_set()  # run_one is woken

    @pytest.mark.asyncio
    async def test_go_prev_at_start_is_noop(self, practice_db):
        ps = self._session(practice_db)
        ps._segment_at_cursor()            # cursor 0
        ps._current_state_path = "s.state"
        await ps.go_prev()
        assert ps._cursor == 0 and ps._nav_pending is False

    @pytest.mark.asyncio
    async def test_skip_next_advances_cursor(self, practice_db):
        ps = self._session(practice_db)
        ps._segment_at_cursor()            # cursor 0
        ps._current_state_path = "s.state"
        await ps.skip_next()
        assert ps._cursor == 1 and ps._nav_pending is True

    @pytest.mark.asyncio
    async def test_nav_ignored_when_not_armed_or_paused(self, practice_db):
        ps = self._session(practice_db)
        ps._segment_at_cursor()
        ps._current_state_path = None       # not armed
        await ps.skip_next()
        assert ps._cursor == 0 and ps._nav_pending is False
        ps._current_state_path = "s.state"; ps.paused = True   # paused
        await ps.skip_next()
        assert ps._cursor == 0 and ps._nav_pending is False
```

NOTE: these reference helpers `_segment_at_cursor`, `_advance_after_completion`, `go_prev`, `skip_next`, and fields `_history`, `_cursor`, `_nav_pending` defined in Step 3. The `practice_db` fixture seeds at least one practicable segment (the existing run_one tests rely on it).

- [ ] **Step 2: Run — verify fail**

Run: `python -m pytest tests/unit/test_practice.py::TestSegmentNavigation -q`
Expected: FAIL — attributes/methods don't exist yet.

- [ ] **Step 3: Implement history + cursor + nav in `python/spinlab/practice.py`**

(a) Add a module-level `_HistoryEntry` dataclass after the imports (add `from dataclasses import dataclass` to the imports):

```python
@dataclass(frozen=True)
class _HistoryEntry:
    """One visited segment: the load command to (re)send + the allocator that
    originally surfaced it (carried into the recorded attempt)."""
    load_cmd: PracticeLoadCmd
    allocator: str | None
```

(b) In `__init__`, after the Practice-Pause state block, add:

```python
        # --- Segment history navigation (R+left/right) ----------------------
        # Browser-style: _history is the ordered segments loaded this session;
        # _cursor indexes the current one. A completed attempt advances the
        # cursor forward (+1); a fresh scheduler pick is appended when the
        # cursor reaches the end. Nav (go_prev/skip_next) moves the cursor and
        # DROPS the in-flight attempt (the pause disarm path, nothing recorded),
        # then wakes run_one via _nav_pending + the existing _result_event.
        self._history: list[_HistoryEntry] = []
        self._cursor: int = 0
        self._nav_pending: bool = False
```

(c) Add these methods (place them just before `run_one`):

```python
    def _build_history_entry(self, picked) -> _HistoryEntry:
        """Build the load command + allocator for a freshly-picked segment."""
        expected_time_ms = None
        sel_out = picked.model_outputs.get(picked.selected_model)
        if sel_out and sel_out.total.expected_ms is not None and sel_out.total.expected_ms > 0:
            expected_time_ms = int(sel_out.total.expected_ms)
        label = picked.description
        if not label:
            start = "start" if picked.start_type == "entrance" else f"cp{picked.start_ordinal}"
            end = "goal" if picked.end_type == "goal" else f"cp{picked.end_ordinal}"
            label = f"L{picked.level_number} {start} > {end}"
        assert picked.state_path is not None  # scheduler only picks segments with save states
        load_cmd = PracticeLoadCmd(
            id=picked.segment_id,
            state_path=picked.state_path,
            description=label,
            end_type=picked.end_type,
            expected_time_ms=expected_time_ms,
            auto_advance_delay_ms=self.auto_advance_delay_ms,
            death_penalty_ms=self.death_penalty_ms,
        )
        return _HistoryEntry(load_cmd=load_cmd, allocator=self.scheduler.last_chosen_allocator)

    def _segment_at_cursor(self) -> _HistoryEntry | None:
        """Return the history entry to load this cycle. Appends a fresh
        scheduler pick when the cursor is at/past the end; skips forward over
        any stale revisit target whose state file has since vanished. Returns
        None when the scheduler has nothing left."""
        while True:
            if self._cursor >= len(self._history):
                picked = self.scheduler.pick_next()
                if picked is None:
                    return None
                self._history.append(self._build_history_entry(picked))
            entry = self._history[self._cursor]
            if os.path.exists(entry.load_cmd.state_path):
                return entry
            log.warn(
                logger, "practice: history segment state missing — skipping",
                segment_id=entry.load_cmd.id, state_path=entry.load_cmd.state_path,
            )
            self._cursor += 1

    def _advance_after_completion(self) -> None:
        """A real completion walks the cursor forward one (fresh pick at end)."""
        self._cursor += 1

    def _nav_ok(self) -> bool:
        if not self.is_running or self._current_state_path is None or self.paused:
            logger.info("practice: nav ignored — no attempt in flight / paused")
            return False
        return True

    async def _begin_nav(self) -> None:
        """Drop the in-flight attempt (disarm, nothing recorded) and wake
        run_one, which then loads the segment now at the cursor."""
        self._nav_pending = True
        self._current_episode_id = None
        self._current_state_path = None  # don't reload-on-death the abandoned seg
        await self.emu.send_command(PracticePauseCmd())
        self._result_event.set()
        logger.info("practice: nav -> cursor=%d", self._cursor)

    async def skip_next(self) -> None:
        """R+right: abandon the in-flight attempt and advance to the next
        segment (forward through history, or a fresh pick at the end)."""
        if not self._nav_ok():
            return
        self._cursor += 1
        await self._begin_nav()

    async def go_prev(self) -> None:
        """R+left: abandon the in-flight attempt and reload the previous segment
        in the visit history. No-op at the start of history."""
        if not self._nav_ok():
            return
        if self._cursor <= 0:
            logger.info("practice: prev ignored — at start of history")
            return
        self._cursor -= 1
        await self._begin_nav()
```

(d) Replace `run_one`'s body. Swap the inline scheduler-pick + cmd-build (the block from `picked = self.scheduler.pick_next()` through the `await self.emu.send_command(self._current_load_cmd)`) so it loads the cursor's entry, and handle the nav wake. The new `run_one`:

```python
    async def run_one(self) -> bool:
        """Run one load-send-receive cycle. Returns False when no segments are
        available. A nav command mid-attempt drops it and returns True so the
        loop immediately loads the cursor's (now different) segment."""
        entry = self._segment_at_cursor()
        if entry is None:
            logger.info("practice: no segments available — ending loop")
            return False

        cmd = entry.load_cmd
        self._last_allocator = entry.allocator
        self._current_episode_id = None
        self.current_segment_id = cmd.id
        self._current_state_path = cmd.state_path
        self._current_load_cmd = cmd
        logger.info("practice: loading segment=%s (cursor=%d/%d) state=%s",
                    cmd.id, self._cursor, len(self._history) - 1, cmd.state_path)

        await self.emu.send_command(cmd)
        if self.on_segment_load is not None:
            self.on_segment_load(cmd.id)

        self._result_event.clear()
        self._result_data = None
        self._nav_pending = False

        load_timeouts = 0
        while self.is_running and self.emu.is_connected:
            try:
                await asyncio.wait_for(self._result_event.wait(), timeout=SEGMENT_LOAD_TIMEOUT_S)
                break
            except asyncio.TimeoutError:
                load_timeouts += 1
                if load_timeouts == 1:
                    log.info(
                        logger, "practice: waiting for attempt result",
                        segment_id=cmd.id, timeout_s=SEGMENT_LOAD_TIMEOUT_S,
                    )
                continue

        if self._nav_pending:
            # A nav command woke us: the in-flight attempt was already dropped
            # (disarmed in _begin_nav) and the cursor moved. Don't process a
            # result — the loop's next run_one loads the cursor's segment.
            self._nav_pending = False
            self.current_segment_id = None
            return True

        if self._result_data is not None:
            self._process_result(self._result_data)
            self._advance_after_completion()
        else:
            log.info(
                logger, "practice: attempt loop exited without result",
                segment_id=cmd.id,
                is_running=self.is_running,
                emu_connected=self.emu.is_connected,
                load_timeouts=load_timeouts,
            )

        self.current_segment_id = None
        return True
```

> Pause is unaffected: `toggle_pause` never sets `_result_event`, so it doesn't wake `run_one`; nav sets `_nav_pending` + `_result_event` and is handled in the new nav branch before result processing.

- [ ] **Step 4: Run — verify the nav tests pass**

Run: `python -m pytest tests/unit/test_practice.py::TestSegmentNavigation -q`
Expected: PASS.

- [ ] **Step 5: Run the WHOLE practice suite — fix any fallout**

Run: `python -m pytest tests/unit/test_practice.py tests/unit/test_practice_coverage.py -q`
Expected: PASS. The existing `run_one`/pause tests must still pass — `run_one` still sends a `PracticeLoadCmd`, sets `current_segment_id`, fires `on_segment_load`, and processes the delivered result (now also advancing the cursor). If any assert on internal pick details, adjust to the history path (the load cmd is now `entry.load_cmd`; behavior is identical for a single segment).

- [ ] **Step 6: Statics**

Run: `ruff check python/spinlab/practice.py` and `npx pyright python/spinlab/practice.py` (report pre-existing vs introduced; fix only NEW).

- [ ] **Step 7: Commit**

```bash
git add python/spinlab/practice.py tests/unit/test_practice.py
git commit -m "feat(practice): segment history nav (cursor + go_prev/skip_next)"
```

---

### Task 3: SessionManager dispatch for prev/next_segment

**Files:**
- Modify: `python/spinlab/session_manager.py`
- Test: `tests/unit/test_session_manager.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_session_manager.py`:

```python
class TestSegmentNavCommands:
    @pytest.mark.asyncio
    async def test_next_segment_calls_skip_next_in_practice(self, db, emu):
        from unittest.mock import AsyncMock
        from spinlab.protocol import ControllerCommandEvent
        sm = make_sm(db, emu)
        sm.mode = Mode.PRACTICE
        sm.practice_session = AsyncMock()
        await sm.route_event(ControllerCommandEvent(command="next_segment"))
        sm.practice_session.skip_next.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_prev_segment_calls_go_prev_in_practice(self, db, emu):
        from unittest.mock import AsyncMock
        from spinlab.protocol import ControllerCommandEvent
        sm = make_sm(db, emu)
        sm.mode = Mode.PRACTICE
        sm.practice_session = AsyncMock()
        await sm.route_event(ControllerCommandEvent(command="prev_segment"))
        sm.practice_session.go_prev.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_nav_ignored_outside_practice(self, db, emu):
        from unittest.mock import AsyncMock
        from spinlab.protocol import ControllerCommandEvent
        sm = make_sm(db, emu)
        sm.mode = Mode.IDLE
        sm.practice_session = AsyncMock()
        await sm.route_event(ControllerCommandEvent(command="next_segment"))
        await sm.route_event(ControllerCommandEvent(command="prev_segment"))
        sm.practice_session.skip_next.assert_not_awaited()
        sm.practice_session.go_prev.assert_not_awaited()
```

- [ ] **Step 2: Run — verify fail**

Run: `python -m pytest tests/unit/test_session_manager.py::TestSegmentNavCommands -q`
Expected: FAIL — next/prev_segment are unknown commands (warning + return).

- [ ] **Step 3: Add the dispatch branches**

In `python/spinlab/session_manager.py`'s `_handle_controller_command`, add two branches before the `else:`:

```python
        elif event.command == "next_segment":
            if self.mode == Mode.PRACTICE and self.practice_session:
                await self.practice_session.skip_next()
        elif event.command == "prev_segment":
            if self.mode == Mode.PRACTICE and self.practice_session:
                await self.practice_session.go_prev()
```

So the handler reads: `if pause ... elif toggle_practice ... elif next_segment ... elif prev_segment ... else warn+return`, then `await self._notify_sse()`.

- [ ] **Step 4: Run — verify pass + no regression**

Run: `python -m pytest tests/unit/test_session_manager.py -q`
Expected: PASS (the new nav tests + existing pause/toggle tests).

- [ ] **Step 5: Statics + commit**

```bash
ruff check python/spinlab/session_manager.py
git add python/spinlab/session_manager.py tests/unit/test_session_manager.py
git commit -m "feat(session): dispatch R+left/right to practice segment nav"
```

---

### Task 4: Emulator confirmation — d-pad reads + dispatches on real RA

**Files:**
- Create: `tests/integration/scenarios/menu_nav.poke`
- Test: `tests/integration/test_transitions.py`

- [ ] **Step 1: Create the scenario**

Create `tests/integration/scenarios/menu_nav.poke`:

```
# menu_nav — hold R ($17 0x10), tap Right ($15 0x01) -> 'next_segment'. Confirms
# the $15 d-pad bits read + dispatch on real RA. Right is not held at the open
# frame, so it's a clean rising edge.
settle: 30

1: controller_held=0x10
5: controller_held_1=0x01
```

(`controller_held_1` is already in the integration `ADDR_MAP` from Phase 2.)

- [ ] **Step 2: Write the failing emulator test**

In `tests/integration/test_transitions.py`, append:

```python
async def test_r_menu_next_segment_command(run_scenario):
    """Hold R, tap Right on real RA -> ControllerCommandEvent('next_segment').
    Confirms the $15 d-pad bits read + dispatch on real RA."""
    from spinlab.protocol import ControllerCommandEvent
    events = await run_scenario("menu_nav.poke")
    cmds = [e for e in events if isinstance(e, ControllerCommandEvent)]
    assert len(cmds) == 1, f"expected 1 command, got {cmds}"
    assert cmds[0].command == "next_segment"
```

- [ ] **Step 3: Run it (requires RA)**

Run: `python -m pytest tests/integration/test_transitions.py::test_r_menu_next_segment_command -m emulator -q`
Expected: PASS. A `SKIPPED` is a FAILURE — surface why. If it fails (no/wrong command), the d-pad bit/byte is off on real RA — report the full event list.

Then the full transitions file: `python -m pytest tests/integration/test_transitions.py -m emulator -q` — all pass.

- [ ] **Step 4: Commit**

```bash
git add tests/integration/scenarios/menu_nav.poke tests/integration/test_transitions.py
git commit -m "test(emulator): confirm d-pad reads + R+right next_segment on real RA"
```

---

### Task 5: Verification gate

- [ ] **Step 1: Fast suite + statics (scope ruff to touched files)**

```bash
python -m pytest -m "not emulator" -q
npx pyright python/spinlab/practice.py python/spinlab/session_manager.py python/spinlab/retroarch/menu_detector.py
ruff check python/spinlab/practice.py python/spinlab/session_manager.py python/spinlab/retroarch/menu_detector.py
```
Expected: green. (Do NOT `ruff check python/spinlab/` — that sweeps ~103 pre-existing vendored `_segments_v07`/dashboard lint, unrelated to this work.)

- [ ] **Step 2: Full suite (incl. emulator) — the merge gate**

```bash
python -m pytest
```
Expected: all pass, **zero skips**. The new `menu_nav` emulator test must run.

- [ ] **Step 3: Manual smoke (RA + dashboard)**

In practice, hold R + tap Right → loads a different segment; hold R + tap Left → reloads the one before; clearing a re-practiced segment walks forward. Pause (R+X) and Toggle (R+Y) still work, and nav during pause is ignored.

---

## Self-review notes (checked against the spec, Phase 3 section)

- **History stack + cursor on `PracticeSession`** → Task 2 (`_history`, `_cursor`, `_HistoryEntry`).
- **Normal completion advances forward (cursor+1; fresh pick at end)** → Task 2 (`_advance_after_completion`, `_segment_at_cursor`). Decision 1.
- **R+← prev / R+→ next, both abort the current attempt (drop, nothing recorded — reuse the pause disarm path)** → Task 2 (`go_prev`/`skip_next`/`_begin_nav`). Decision 2.
- **Reuse the "abort + load segment" machinery shared with pause** → `_begin_nav` sends `PracticePauseCmd` (the same disarm); `run_one` loads the cursor's entry.
- **Open mechanics now decided:** history stores `_HistoryEntry(load_cmd, allocator)` (decision 3); stale revisit targets skip forward (decision 4); nav gated to armed-and-not-paused (decision 5).
- **Detector + dispatch + emulator** for the two d-pad verbs → Tasks 1, 3, 4. D-pad bits per decision 6; real-RA d-pad confirmation in Task 4.
