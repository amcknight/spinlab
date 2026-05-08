# Backend Layering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move save-on-event and practice reload-on-death out of `RetroArchOrchestrator` into the application-layer controllers (`ReferenceController`, `ColdFillController`, `PracticeSession`). Orchestrator becomes pure IPC again. Drops the duplicated `_recording` flag, the per-save basename refresh, and the noisy invalidate-combo log.

**Architecture:** `EmuBackend` Protocol grows two new operations: `async save_state(segment_id)` and `async load_state(state_path)`. RA's orchestrator implements both as worker-thread wrappers around `StateIO`. Mesen's `TcpManager` implements both as no-ops (Lua handles state I/O autonomously). Capture controllers and `PracticeSession` call these directly when they decide a save/load is warranted, removing the orchestrator's knowledge of mode and recording state.

**Tech Stack:** Python 3.11+, asyncio. No new deps.

**Sequencing:** This plan **depends on Plan A** (event pipeline collapse) landing first. Without Plan A, the controllers below would receive `dict` events from `route_event`; with Plan A, they could receive typed events. We keep dict-input on `route_event` for cross-backend uniformity, but the typed protocol classes are still used internally (recorder, capture).

---

## File Structure

**Modified:**
- `python/spinlab/emu_backend.py` — add `save_state` and `load_state` to the `EmuBackend` Protocol.
- `python/spinlab/tcp_manager.py` — add no-op `save_state` / `load_state` (Lua handles).
- `python/spinlab/retroarch/orchestrator.py` — implement `save_state` / `load_state`. Delete `_recording`, `_practice_state_path`, `_maybe_save_state_for`, `_save_state_async`, `_save_state_sync`, `_maybe_reload_state_on_death`, `_reload_state_async`, `_on_practice_attempt_result`. Reference start/stop become near-true no-ops (still emit synthetic `rec_saved` for cross-backend uniformity). Downgrade invalidate-combo log to debug.
- `python/spinlab/retroarch/state_io.py` — drop `_refresh_game_basename_from_ra` from the save path. Document that ROM hot-swap requires reconnect.
- `python/spinlab/capture/reference.py` — `handle_entrance` and `handle_checkpoint` become async; trigger `await self.tcp.save_state(seg_id)` when recording.
- `python/spinlab/capture/cold_fill.py` — `handle_spawn` triggers `await self.tcp.save_state(seg_id)` so the cold state is captured under any backend.
- `python/spinlab/practice.py` — handle Death and LevelExit(abort) events to drive reload via `await self.tcp.load_state(state_path)`. Track current segment's `state_path` after `PracticeLoadCmd` is sent.
- `python/spinlab/session_manager.py` — route Death and LevelExit events to `PracticeSession` when in PRACTICE mode. Make `_handle_level_entrance`/`_handle_checkpoint` await the now-async controller methods.

**Note on `segment_id_for_event`:** added in #4 (already on `StateIO`). The capture controllers compute their save-key directly from event fields (`f"entrance_{level}_{room}"`) — they don't reach into the orchestrator. That keeps the naming-scheme single-source on `StateIO` for path resolution, while the controllers know how to derive the same id from event fields. Either is fine because the names are pure functions of event data. We prefer the controllers using `StateIO`'s helper if available, but under Mesen there is no `StateIO`, so the simplest approach is for capture controllers to compute the id themselves.

To keep DRY: extract the naming logic into a free helper in `python/spinlab/capture/segment_naming.py` (or a `staticmethod` on a small dataclass). Both `StateIO.segment_id_for_event` and the capture controllers go through it.

---

## Task 1: Add `save_state` / `load_state` to the `EmuBackend` Protocol

**Files:**
- Modify: `python/spinlab/emu_backend.py`
- Test: covered by Tasks 4–6 below (pure Protocol — no behavior to test directly)

- [ ] **Step 1: Edit `EmuBackend`**

```python
"""EmuBackend — duck-typed surface shared by TcpManager and RetroArchOrchestrator."""
from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable


@runtime_checkable
class EmuBackend(Protocol):
    on_disconnect: Callable | None

    @property
    def is_connected(self) -> bool: ...

    async def connect(self, timeout: float = ...) -> bool: ...

    async def disconnect(self) -> None: ...

    async def send_command(self, cmd: object) -> None: ...

    async def recv_event(self, timeout: float | None = ...) -> dict | None: ...

    async def save_state(self, segment_id: str) -> None:
        """Persist a savestate file for the given segment id.

        Under RetroArch the orchestrator triggers an NCI SAVE_STATE and moves
        the resulting file into SpinLab's segment-keyed directory. Under
        Mesen this is a no-op because Lua writes states autonomously when
        it observes save-eligible events; Python does not need to act.
        """
        ...

    async def load_state(self, state_path: str) -> None:
        """Load a savestate file from an absolute path.

        Under RetroArch the orchestrator copies the file into RA's reserved
        slot and fires LOAD_STATE_SLOT. Under Mesen this is a no-op because
        Lua's practice loop loads states autonomously after every
        ``practice_load`` command and on every detected death.
        """
        ...
```

- [ ] **Step 2: Commit**

```bash
git add python/spinlab/emu_backend.py
git commit -m "feat(emu_backend): add save_state and load_state to Protocol"
```

---

## Task 2: Implement `save_state` / `load_state` on `TcpManager` (no-op)

**Files:**
- Modify: `python/spinlab/tcp_manager.py`
- Test: `tests/unit/test_tcp_manager.py` (extend if exists, else skip)

- [ ] **Step 1: Add no-op implementations**

```python
class TcpManager:
    # ... existing code ...

    async def save_state(self, segment_id: str) -> None:
        """No-op: Lua writes states autonomously under the Mesen backend."""
        return None

    async def load_state(self, state_path: str) -> None:
        """No-op: Lua handles state loading via practice_load and pending_loads."""
        return None
```

- [ ] **Step 2: Run full suite**

Run: `python -m pytest --tb=short -q`
Expected: PASS — no behavior change.

- [ ] **Step 3: Commit**

```bash
git add python/spinlab/tcp_manager.py
git commit -m "feat(tcp_manager): no-op save_state/load_state stubs (Lua owns state I/O)"
```

---

## Task 3: Implement `save_state` / `load_state` on `RetroArchOrchestrator`

**Files:**
- Modify: `python/spinlab/retroarch/orchestrator.py`
- Test: `tests/unit/retroarch/test_orchestrator.py` — replace existing `test_reference_recording_triggers_save_on_level_entrance` and `test_cold_fill_spawn_always_saves...` with direct tests on `save_state` / `load_state`.

- [ ] **Step 1: Write failing tests for the new methods**

```python
@pytest.mark.asyncio
async def test_save_state_runs_state_io_in_thread():
    orch, client, state_io, poller, _ = _build_orchestrator()
    await orch.connect()
    await orch.events.get()
    await orch.save_state("seg_123")
    assert state_io.saved_segments == ["seg_123"]
    await orch.disconnect()


@pytest.mark.asyncio
async def test_load_state_runs_state_io_and_marks_loaded():
    orch, client, state_io, poller, _ = _build_orchestrator()
    await orch.connect()
    await orch.events.get()
    await orch.load_state("/some/path/file.state")
    assert state_io.load_path_calls == ["/some/path/file.state"]
    assert poller.mark_state_loaded_calls >= 1
    await orch.disconnect()
```

- [ ] **Step 2: Run failing tests**

Run: `pytest tests/unit/retroarch/test_orchestrator.py::test_save_state_runs_state_io_in_thread -v`
Expected: FAIL — method doesn't exist.

- [ ] **Step 3: Add the methods**

```python
class RetroArchOrchestrator:
    # ... existing code ...

    async def save_state(self, segment_id: str) -> None:
        """Run StateIO.save_segment_state in a worker thread."""
        await asyncio.to_thread(self._state_io.save_segment_state, segment_id)

    async def load_state(self, state_path: str) -> None:
        """Run StateIO.load_state_from_path in a worker thread; mark prev-snapshot."""
        await asyncio.to_thread(self._state_io.load_state_from_path, state_path)
        self._poller.mark_state_loaded()
```

- [ ] **Step 4: Run tests to verify**

Run: `pytest tests/unit/retroarch/test_orchestrator.py::test_save_state_runs_state_io_in_thread tests/unit/retroarch/test_orchestrator.py::test_load_state_runs_state_io_and_marks_loaded -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/retroarch/orchestrator.py tests/unit/retroarch/test_orchestrator.py
git commit -m "feat(retroarch): orchestrator save_state/load_state pump StateIO via to_thread"
```

---

## Task 4: Extract segment-id naming helper for shared use

**Files:**
- Create: `python/spinlab/capture/segment_naming.py`
- Modify: `python/spinlab/retroarch/state_io.py:segment_id_for_event` to delegate.
- Test: `tests/unit/capture/test_segment_naming.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/capture/test_segment_naming.py
from spinlab.capture.segment_naming import segment_id_for_event
from spinlab.protocol import (
    CheckpointEvent,
    DeathEvent,
    LevelEntranceEvent,
    SpawnEvent,
)


def test_entrance_id():
    ev = LevelEntranceEvent(level=5, room=2)
    assert segment_id_for_event(ev) == "entrance_5_2"


def test_checkpoint_hot_id():
    ev = CheckpointEvent(level_num=5, cp_ordinal=1)
    assert segment_id_for_event(ev) == "cp_5_1_hot"


def test_spawn_with_segment_id_passes_through():
    ev = SpawnEvent(level_num=5, segment_id="my_seg_id")
    assert segment_id_for_event(ev) == "my_seg_id"


def test_spawn_without_segment_id_returns_none():
    ev = SpawnEvent(level_num=5)
    assert segment_id_for_event(ev) is None


def test_death_event_returns_none():
    assert segment_id_for_event(DeathEvent()) is None
```

- [ ] **Step 2: Create the helper**

```python
"""Segment-id naming for save state files. Single source of truth.

Both the RA-backend `StateIO.segment_id_for_event` and the capture
controllers' save-on-event hooks go through this helper so the naming
scheme has exactly one writer.
"""
from __future__ import annotations

from spinlab.protocol import (
    CheckpointEvent,
    LevelEntranceEvent,
    SpawnEvent,
)


def segment_id_for_event(event) -> str | None:
    """Return the segment-id key for a save-eligible event, or None.

    Naming conventions (match lua/spinlab.lua's filename layout):
      - LevelEntranceEvent  -> "entrance_<level>_<room>"
      - CheckpointEvent     -> "cp_<level>_<ordinal>_hot"
      - SpawnEvent          -> the event's segment_id (cold-fill captures)
    """
    if isinstance(event, LevelEntranceEvent):
        return f"entrance_{event.level}_{event.room}"
    if isinstance(event, CheckpointEvent):
        return f"cp_{event.level_num}_{event.cp_ordinal}_hot"
    if isinstance(event, SpawnEvent) and event.segment_id:
        return event.segment_id
    return None
```

- [ ] **Step 3: Run tests**

Run: `pytest tests/unit/capture/test_segment_naming.py -v`
Expected: PASS.

- [ ] **Step 4: Update `StateIO.segment_id_for_event` to delegate**

```python
# python/spinlab/retroarch/state_io.py
from spinlab.capture.segment_naming import segment_id_for_event as _segment_id_for_event


class StateIO:
    # ...

    def segment_id_for_event(self, event) -> str | None:
        return _segment_id_for_event(event)
```

(Or just import directly at call sites and remove the method — but leaving the method preserves the public surface and lets `StateIO` be self-contained for tests.)

- [ ] **Step 5: Run full suite**

Run: `python -m pytest --tb=short -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/capture/segment_naming.py python/spinlab/retroarch/state_io.py tests/unit/capture/test_segment_naming.py
git commit -m "refactor(capture): extract segment_id_for_event as shared helper"
```

---

## Task 5: Move save-on-event to `ReferenceController`

**Files:**
- Modify: `python/spinlab/capture/reference.py`
- Modify: `python/spinlab/session_manager.py` (`_handle_level_entrance` / `_handle_checkpoint` await the now-async controller methods)
- Test: `tests/unit/capture/test_reference_controller.py` (add tests asserting save_state called)

- [ ] **Step 1: Write failing tests**

Use the existing `db(tmp_path)` fixture pattern from
`tests/unit/capture/test_reference.py:31`:

```python
# add to tests/unit/capture/test_reference.py (or new test_reference_save_on_event.py)
import pytest

from spinlab.capture import ReferenceController
from spinlab.db import Database
from spinlab.protocol import CheckpointEvent, LevelEntranceEvent


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "test.db")


class _FakeBackend:
    def __init__(self):
        self.is_connected = True
        self.save_calls = []
        self.on_disconnect = None

    async def save_state(self, seg_id):
        self.save_calls.append(seg_id)

    async def load_state(self, path): ...
    async def send_command(self, cmd): ...
    async def connect(self, timeout=5.0): return True
    async def disconnect(self): ...
    async def recv_event(self, timeout=None): return None


@pytest.mark.asyncio
async def test_handle_entrance_saves_state_when_recording(db):
    tcp = _FakeBackend()
    rc = ReferenceController(db, tcp)
    rc._enter_recording("run_x", "sess_x")
    ev = LevelEntranceEvent(level=5, room=2)
    await rc.handle_entrance(ev)
    assert tcp.save_calls == ["entrance_5_2"]


@pytest.mark.asyncio
async def test_handle_entrance_does_not_save_when_idle(db):
    tcp = _FakeBackend()
    rc = ReferenceController(db, tcp)
    ev = LevelEntranceEvent(level=5, room=2)
    await rc.handle_entrance(ev)
    assert tcp.save_calls == []


@pytest.mark.asyncio
async def test_handle_checkpoint_saves_hot_state_when_recording(db):
    tcp = _FakeBackend()
    rc = ReferenceController(db, tcp)
    rc._enter_recording("run_x", "sess_x")
    ev = CheckpointEvent(level_num=5, cp_ordinal=1)
    await rc.handle_checkpoint(ev, "game_id")
    assert tcp.save_calls == ["cp_5_1_hot"]
```

- [ ] **Step 2: Run failing tests**

Run: `pytest tests/unit/capture/test_reference_controller.py -v -k "save_state"`
Expected: FAIL — handle_entrance is sync and doesn't call save_state.

- [ ] **Step 3: Update `ReferenceController.handle_entrance` and `handle_checkpoint`**

```python
# python/spinlab/capture/reference.py
from .segment_naming import segment_id_for_event


class ReferenceController:
    # ...

    async def handle_entrance(self, event: LevelEntranceEvent) -> None:
        logger.info("capture: entrance level=%s", event.level)
        if self.is_recording:
            seg_id = segment_id_for_event(event)
            if seg_id:
                try:
                    await self.tcp.save_state(seg_id)
                except Exception:
                    logger.exception(
                        "save_state failed for entrance event seg_id=%r", seg_id,
                    )
        self.recorder.handle_entrance(event)

    async def handle_checkpoint(
        self, event: CheckpointEvent, game_id: str,
    ) -> None:
        logger.info("capture: checkpoint level=%s cp=%s",
                    event.level_num, event.cp_ordinal)
        if self.is_recording:
            seg_id = segment_id_for_event(event)
            if seg_id:
                try:
                    await self.tcp.save_state(seg_id)
                except Exception:
                    logger.exception(
                        "save_state failed for checkpoint event seg_id=%r", seg_id,
                    )
        self.recorder.handle_checkpoint(event, game_id, self.db,
                                        self.condition_registry)
```

- [ ] **Step 4: Update `SessionManager` to await the now-async methods**

```python
# python/spinlab/session_manager.py

async def _handle_level_entrance(self, event: LevelEntranceEvent) -> None:
    if self.mode not in (Mode.REFERENCE, Mode.REPLAY):
        return
    await self.capture.handle_entrance(event)
    await self._notify_sse()

async def _handle_checkpoint(self, event: CheckpointEvent) -> None:
    if self.mode not in (Mode.REFERENCE, Mode.REPLAY):
        return
    await self.capture.handle_checkpoint(event, self.require_game())
    await self._notify_sse()
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/unit/capture/ tests/unit/test_session_manager*.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/capture/reference.py python/spinlab/session_manager.py tests/unit/capture/
git commit -m "refactor(capture): ReferenceController owns save-on-event"
```

---

## Task 6: Move cold-fill spawn save into `ColdFillController`

**Files:**
- Modify: `python/spinlab/capture/cold_fill.py`
- Test: `tests/unit/capture/test_cold_fill.py`

The cold-fill controller already receives the Spawn event with `state_captured=True` and `state_path` stamped. Today, the orchestrator wrote the file before stamping. Now the controller must request the save itself.

- [ ] **Step 1: Write failing test**

Reuse the `db(tmp_path)` fixture pattern from Task 5.

```python
@pytest.mark.asyncio
async def test_handle_spawn_saves_state_via_backend(db):
    tcp = _FakeBackend()
    cfc = ColdFillController(db, tcp)
    # ... arrange queue with one segment, cold_waypoint_id set ...
    ev = SpawnEvent(
        level_num=5, segment_id="seg_abc",
        is_cold_cp=True, state_captured=True,
        state_path="/states/game/seg_abc.state",
    )
    done = await cfc.handle_spawn(ev)
    assert tcp.save_calls == ["seg_abc"]
```

- [ ] **Step 2: Update `handle_spawn`**

```python
async def handle_spawn(self, event: SpawnEvent) -> bool:
    if not self.current:
        logger.warning("cold_fill: spawn received but no current segment")
        return False
    if not event.state_captured or not event.state_path:
        logger.info("cold_fill: spawn without state_captured — ignoring (state_path=%s)",
                    event.state_path)
        return False
    seg_id = event.segment_id or self.current
    try:
        await self.tcp.save_state(seg_id)
    except Exception:
        logger.exception("cold_fill: save_state failed for seg_id=%r", seg_id)
        return False
    # ... existing DB-write + queue-advance logic unchanged ...
```

- [ ] **Step 3: Run tests**

Run: `pytest tests/unit/capture/test_cold_fill.py -v`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add python/spinlab/capture/cold_fill.py tests/unit/capture/test_cold_fill.py
git commit -m "refactor(capture): ColdFillController triggers save_state itself"
```

---

## Task 7: Move practice reload-on-death into `PracticeSession`

**Files:**
- Modify: `python/spinlab/practice.py`
- Modify: `python/spinlab/session_manager.py` (route Death + LevelExit(abort) to practice)
- Test: `tests/unit/test_practice.py` (add test asserting load_state on death)

The current orchestrator-side reload-on-death (`_maybe_reload_state_on_death`) keys off the orchestrator's cached `_practice_state_path`. We move that into `PracticeSession`: it remembers `_current_state_path` set when sending `PracticeLoadCmd`, and reloads on death/abort events.

- [ ] **Step 1: Write failing tests**

```python
@pytest.mark.asyncio
async def test_practice_session_reloads_state_on_death(...):
    ps = PracticeSession(tcp=fake_backend, db=db, game_id="g")
    ps._current_state_path = "/states/seg_x.state"
    ps.is_running = True
    await ps.handle_death()
    assert fake_backend.load_calls == ["/states/seg_x.state"]


@pytest.mark.asyncio
async def test_practice_session_reloads_on_level_exit_abort(...):
    ps = PracticeSession(tcp=fake_backend, db=db, game_id="g")
    ps._current_state_path = "/states/seg_y.state"
    ps.is_running = True
    await ps.handle_level_exit_abort()
    assert fake_backend.load_calls == ["/states/seg_y.state"]
```

- [ ] **Step 2: Update `PracticeSession`**

```python
class PracticeSession:
    def __init__(self, tcp: "EmuBackend", db: "Database", game_id: str, ...):
        # ...
        # _current_state_path doubles as the "armed for reload-on-death"
        # flag: non-None means an attempt is in flight and a Death event
        # should trigger a reload. Cleared the moment attempt_result is
        # observed (in receive_result, not at run_one cleanup) so a Death
        # arriving in the small window between result emission and run_one
        # tear-down doesn't cause a spurious reload.
        self._current_state_path: str | None = None
        # ...

    async def run_one(self) -> bool:
        picked = self.scheduler.pick_next()
        if picked is None:
            return False
        # ...
        self._current_state_path = picked.state_path
        await self.tcp.send_command(PracticeLoadCmd(...))
        # ... existing wait-for-result loop ...
        # No longer clear _current_state_path here — receive_result owns that.
        return True

    def receive_result(self, event: AttemptResultEvent) -> None:
        # Clear armed flag FIRST so any in-flight Death/abort handling
        # already in the queue doesn't try to reload on a finished attempt.
        self._current_state_path = None
        self._result_data = event
        self._result_event.set()

    async def handle_death(self) -> None:
        """Called by SessionManager when a Death event arrives during PRACTICE mode."""
        path = self._current_state_path
        if not path:
            return
        try:
            await self.tcp.load_state(path)
        except Exception:
            logger.exception("practice: load_state on death failed (path=%s)", path)

    async def handle_level_exit_abort(self) -> None:
        """Same as handle_death — pit-falls / death-falls don't fire a Death frame."""
        await self.handle_death()
```

- [ ] **Step 3: Update `SessionManager._handle_death` and `_handle_level_exit`**

```python
# session_manager.py

async def _handle_death(self, event: DeathEvent) -> None:
    if self.mode == Mode.COLD_FILL:
        logger.info("death during cold_fill — waiting for respawn")
        return
    if self.mode in (Mode.REFERENCE, Mode.REPLAY):
        self.capture.handle_death(event)
        return
    if self.mode == Mode.PRACTICE and self.practice_session:
        await self.practice_session.handle_death()


async def _handle_level_exit(self, event: LevelExitEvent) -> None:
    if self.mode == Mode.PRACTICE and self.practice_session and event.goal == "abort":
        await self.practice_session.handle_level_exit_abort()
        return
    if self.mode not in (Mode.REFERENCE, Mode.REPLAY):
        return
    self.capture.handle_exit(event, self.require_game())
    await self._notify_sse()
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/test_practice.py tests/unit/test_session_manager*.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/practice.py python/spinlab/session_manager.py tests/unit/test_practice.py
git commit -m "refactor(practice): PracticeSession owns reload-on-death"
```

---

## Task 8: Strip the orchestrator of moved-out responsibilities

**Files:**
- Modify: `python/spinlab/retroarch/orchestrator.py`
- Test: `tests/unit/retroarch/test_orchestrator.py` — drop the now-redundant tests, keep dispatch-table tests.

Now safe to delete because every responsibility has a new home:
- `_recording` flag → ReferenceController owns it via `is_recording`.
- `_practice_state_path` → PracticeSession owns it.
- `_maybe_save_state_for` / `_save_state_async` / `_save_state_sync` → save_state method + ReferenceController/ColdFillController.
- `_maybe_reload_state_on_death` / `_reload_state_async` → load_state method + PracticeSession.
- `_on_practice_attempt_result` → inline `self._enqueue_dict` directly into `practice_timing.arm(on_attempt_result=self._enqueue_dict)`.

`_on_reference_start` / `_on_reference_stop` keep emitting the synthetic `rec_saved` event so cross-backend `handle_rec_saved` flow stays consistent. They no longer toggle `_recording`.

- [ ] **Step 1: Delete the moved code**

```python
# orchestrator.py

# Remove these fields from __init__:
# - self._recording = False
# - self._practice_state_path: str | None = None

# Replace _on_practice_load body:
async def _on_practice_load(self, cmd: PracticeLoadCmd) -> None:
    self._state_io.load_state_from_path(cmd.state_path)
    self._poller.mark_state_loaded()
    self._practice_timing.arm(
        segment_id=cmd.id,
        end_type=cmd.end_type,
        death_penalty_ms=cmd.death_penalty_ms,
        auto_advance_delay_ms=cmd.auto_advance_delay_ms,
        on_attempt_result=self._enqueue_dict,
    )

# Drop _on_practice_attempt_result entirely.

# Replace _on_reference_start body:
async def _on_reference_start(self, cmd: ReferenceStartCmd) -> None:
    """No-op under RA: ReferenceController owns recording state and save triggers.

    Kept in the dispatch table so unknown-cmd warnings don't fire.
    """
    logger.info("RA reference start (no-op — controller owns it)")

# Replace _on_reference_stop body — true no-op:
async def _on_reference_stop(self, cmd: ReferenceStopCmd) -> None:
    """No-op under RA. ReferenceController owns the lifecycle now.

    The previous version emitted a synthetic ``rec_saved`` event so
    ``SessionManager.handle_rec_saved`` would fire and store
    ``recorder.rec_path = ""``. That value was only consumed by the
    REPLAY-mode state_builder branch (REPLAY isn't supported under RA),
    so the synthetic emit was workaround-for-nothing. Drop it.
    """

# Remove on_poller_event's calls to _maybe_save_state_for and
# _maybe_reload_state_on_death. Body becomes:
def on_poller_event(self, ev) -> None:
    try:
        d = dataclasses.asdict(ev)
    except TypeError:
        logger.warning(
            "RetroArchOrchestrator: dropping unknown event type %r",
            type(ev).__name__,
        )
        return
    self._practice_timing.observe_event(d)
    self._speed_run_timing.observe_event(d)
    self.events.put_nowait(d)

# Delete: _maybe_save_state_for, _save_state_async, _save_state_sync,
#         _maybe_reload_state_on_death, _reload_state_async.
```

- [ ] **Step 2: Downgrade invalidate-combo log to debug (C2)**

```python
async def _on_set_invalidate_combo(self, cmd: SetInvalidateComboCmd) -> None:
    logger.debug(
        "RetroArchOrchestrator: invalidate combo is dashboard-button only under RA backend; ignoring %r",
        cmd.combo,
    )
```

- [ ] **Step 3: Drop per-save basename refresh (E2)**

```python
# python/spinlab/retroarch/state_io.py
def save_segment_state(self, segment_id: str) -> Path:
    """...

    The basename is set once at orchestrator connect() from RA's GET_STATUS.
    If the user hot-swaps ROMs in RA mid-session, the dashboard does NOT
    auto-detect this — they must reconnect (e.g. by reloading the dashboard
    page or restarting). Trade-off: removes a per-save GET_STATUS round-trip
    that was hot-path overhead.
    """
    if not self._game_basename:
        raise RuntimeError(...)
    pattern = f"{self._game_basename}.state*"
    # ... rest unchanged ...
    # DELETE the call to self._refresh_game_basename_from_ra()

# Also delete the _refresh_game_basename_from_ra method itself
# (no remaining callers).
```

- [ ] **Step 4: Run full suite**

Run: `python -m pytest --tb=short -q`
Expected: PASS. Some orchestrator tests that asserted save-on-event behavior now assert via the controller-level tests added in Tasks 5–7; remove the now-redundant orchestrator tests.

- [ ] **Step 5: Type check**

Run: `npx pyright python/spinlab/retroarch/orchestrator.py python/spinlab/retroarch/state_io.py python/spinlab/capture/ python/spinlab/practice.py python/spinlab/session_manager.py`
Expected: 0 errors.

- [ ] **Step 6: Verify orchestrator size dropped**

Run: `wc -l python/spinlab/retroarch/orchestrator.py`
Expected: ~150 lines smaller than before (was ~575).

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "refactor(retroarch): orchestrator returns to pure IPC

Practice reload-on-death moved to PracticeSession; save-on-event moved
to ReferenceController and ColdFillController. _recording flag deleted
(ReferenceController.is_recording is the single source). Per-save
basename refresh dropped (set once at connect; ROM hot-swap requires
reconnect). Invalidate-combo cmd downgraded to debug log."
```

---

---

## Task 9: Sync the live docs

**Files:**
- Modify: `docs/retroarch-migration/architecture.md`
- Modify: `docs/retroarch-migration/status.md`
- Modify: `docs/ARCHITECTURE.md` (only if event-flow text references the moved methods)

The architecture doc explicitly describes `_maybe_save_state_for` and
`_maybe_reload_state_on_death` in its event-flow section (lines ~60–63).
After Task 8 those methods are gone.

- [ ] **Step 1: Update `architecture.md` event-flow section**

Replace the bullets that mention `_maybe_save_state_for` and
`_maybe_reload_state_on_death` with a description of the new flow:

> 5. `RetroArchOrchestrator.on_poller_event(ev)` is the registered
>    callback. It converts the protocol event to a JSON dict via
>    `dataclasses.asdict`, feeds it to the timing state machines
>    (`PracticeTiming.observe_event` / `SpeedRunTiming.observe_event`),
>    and enqueues onto `self.events`. Save-on-event and practice
>    reload-on-death moved out of the orchestrator in 2026-05-07's
>    backend-layering refactor: `ReferenceController` and
>    `ColdFillController` call `EmuBackend.save_state(seg_id)`;
>    `PracticeSession` calls `EmuBackend.load_state(state_path)` on
>    Death and LevelExit(abort) events.

- [ ] **Step 2: Update `status.md` "Known broken / untested" section**

Replace the diagnostic-logging note about
`_maybe_reload_state_on_death` with: "Diagnostic logging now lives on
`PracticeSession.handle_death`."

- [ ] **Step 3: Run all tests one final time**

Run: `python -m pytest --tb=short -q`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add docs/retroarch-migration/architecture.md docs/retroarch-migration/status.md docs/ARCHITECTURE.md
git commit -m "docs(retroarch): sync architecture/status to post-layering reality"
```

---

## Self-Review

- **Spec coverage:**
  - Move save-on-event out of orchestrator → Tasks 5, 6.
  - Move practice reload-on-death out of orchestrator → Task 7.
  - Drop `_recording` mirror → Task 8.
  - Orchestrator decomposition (B3) → Task 8 strips ~150 lines.
  - Basename ownership (E2) → Task 8 step 3.
  - Invalidate-combo cleanup (C2) → Task 8 step 2.

- **Type consistency:**
  - `EmuBackend.save_state` and `load_state` signatures match what TcpManager/RetroArchOrchestrator implement.
  - `segment_id_for_event` returns `str | None` everywhere.
  - `PracticeSession.handle_death` and `handle_level_exit_abort` are async; SessionManager awaits them.

- **Risks:**
  - **Atomicity of save-on-event:** Today the save runs in a worker thread and `handle_entrance` returns before the file is on disk. With the new flow, `await self.tcp.save_state(seg_id)` blocks `route_event` until the save completes (or its retry budget expires — up to ~3.6s on RA). If this stalls SSE updates noticeably, follow-up: spawn the save task and don't await (fire-and-forget with logging on failure). Initially we await for simpler semantics.
  - **Cold-fill double-save:** the orchestrator was saving on Spawn-with-segment_id; now the controller also saves. After Task 8 the orchestrator no longer saves. Verified the controller path covers the same case.
  - **ROM hot-swap (E2):** removing per-save refresh means a user who switches ROMs in RA mid-session will hit silent timeouts on the next save. We document this in `state_io.py` and `docs/retroarch-migration/status.md`. Mitigation: the dashboard connection re-sets basename from GET_STATUS at every reconnect, so reloading the dashboard page recovers.
  - **`_on_reference_start` becoming a logger-only no-op under RA:** the cmd still flows through the IPC layer for symmetry with Mesen. The `synthetic rec_saved` emit on stop stays for cross-backend uniformity in the controller's lifecycle.
