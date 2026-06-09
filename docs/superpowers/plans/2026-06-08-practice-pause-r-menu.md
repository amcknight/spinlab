# Practice Pause + R-menu Command Layer — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Holding R on controller 1 arms a command "menu"; pressing X dispatches a `pause` command that drops the in-flight practice attempt, freezes the session/savings clock, and reloads the same segment on resume — so idle time never enters the data.

**Architecture:** A new poller-level `ControllerMenuDetector` reads SMW's held-button byte (`$17`) from the per-frame snapshot and emits `ControllerMenuArmedEvent` / `ControllerCommandEvent`. These flow through the existing poller → orchestrator → `SessionManager.route_event` pipeline. The practice pause is handled as a toggle on `PracticeSession`: pause sends a `PracticePauseCmd` (disarms the backend `PracticeTiming` so events are ignored) and records a pause-start; resume re-sends the stashed `PracticeLoadCmd` (reloads + re-arms a fresh attempt) and folds the paused span into a session pause-offset. The route bar freezes its elapsed clock + savings/hr by pinning "now" to the pause-start and subtracting the accumulated offset.

**Tech Stack:** Python 3.11 (FastAPI backend, dataclass protocol events), TypeScript + Vite frontend (codegen'd types from FastAPI OpenAPI), pytest (unit + `emulator` marker via the RA poke harness), vitest (frontend).

**Spec:** `docs/superpowers/specs/2026-06-07-practice-pause-r-menu-design.md`.

## Decisions locked before writing (read these — they diverge from the spec text)

1. **Held byte is `$17` (confirmed).** kaizosplits `reference/.../SMW/Memory.cs:51` maps `0x0017 -> "buttonsHeld2"`, the held **A X L R** byte (R = `0x10`, X = `0x40`). The spec's bit layout is correct.
2. **Read only `$17` (held); defer `$18` (newly-pressed).** Dispatch fires on a **rising edge of the held X bit**, which is the debounced equivalent of "newly pressed" and avoids the double-fire the poke harness would cause if we level-triggered on `$18` (the harness re-asserts poked bytes every frame). R-arm *requires* the held byte anyway (a one-frame "pressed" byte can't be held for 30 frames). `$18` gets added when a command needs true newly-pressed semantics. This is a deliberate, documented deviation from the spec's "read `$17` AND `$18`".
3. **Paused mid-attempt episode: leave already-written rows, just disarm.** (Andrew's call.) Disarming the backend timing means no `AttemptResultEvent` fires for the dropped attempt → no `Attempt` row, and future death/finish events during the pause are ignored. Any `died` `EventAttempt` rows that were persisted *before* the pause stay (they were real deaths). We do **not** add an "incomplete episode" concept or delete rows — episodes are a derived/secondary view; attempts are the real object.
4. **Pause + menu-armed UI lives on the route bar, driven by `RouteSummaryResponse`** (the `/api/games/{id}/live-summary` payload), not `AppState`. The route bar already owns the elapsed/savings clock that must freeze, is the live-practice surface, and is re-fetched on every SSE push. Putting the three new fields there is strictly less plumbing than `AppState` + a second consumer and keeps the frozen-clock math co-located. (Deviation from spec's "AppState push" wording; same SSE-driven update.)
5. **`MemorySnapshot.controller_held` is a trailing field with default `0`.** Transition detection never consults it, so the ~10 existing `MemorySnapshot(...)` construction sites in tests legitimately default it; only `read_snapshot` and the menu detector set it. Avoids churning every snapshot builder.

## File map

| File | Change |
|---|---|
| `python/spinlab/retroarch/addresses.py` | + `ADDR_CONTROLLER_HELD = 0x17` |
| `python/spinlab/retroarch/snapshot.py` | + `controller_held` field + a 7th NCI read of `$17` |
| `python/spinlab/protocol.py` | + `ControllerCommandEvent`, `ControllerMenuArmedEvent`, `PracticePauseCmd` |
| `python/spinlab/retroarch/menu_detector.py` | **new** — `ControllerMenuDetector` |
| `python/spinlab/retroarch/poller.py` | run the menu detector + forward its events |
| `python/spinlab/retroarch/orchestrator.py` | dispatch `PracticePauseCmd` → `_practice_timing.disarm()` |
| `python/spinlab/practice.py` | pause state + `toggle_pause()` + stash load cmd + gate `handle_death` |
| `python/spinlab/system_state.py` | + `menu_armed: bool` |
| `python/spinlab/session_manager.py` | menu/command event handlers + dispatch entries |
| `python/spinlab/api_schemas.py` | `RouteSummaryResponse` + 3 fields |
| `python/spinlab/routes/model.py` | populate the 3 fields in `get_route_summary` |
| `frontend/src/route-bar.ts` | freeze clock, PAUSED badge, R-menu hint |
| `frontend/src/styles.css` (or the live-view CSS file) | `.rb-paused`, `.rb-menu-hint` |
| `tests/integration/addresses.py` | + `"controller_held"` entry |
| `tests/integration/ra_poke_engine.py` | also run `ControllerMenuDetector`, merge events |
| `tests/integration/scenarios/menu_pause.poke` | **new** scenario |
| Tests | menu-detector unit, snapshot unit, poller unit, orchestrator unit, practice pause unit, session-manager unit, route unit, route-bar vitest, emulator poke test |

---

### Task 1: Controller-held snapshot field + `$17` read

**Files:**
- Modify: `python/spinlab/retroarch/addresses.py`
- Modify: `python/spinlab/retroarch/snapshot.py`
- Test: `tests/unit/retroarch/test_snapshot.py`

- [ ] **Step 1: Add the address constant**

In `python/spinlab/retroarch/addresses.py`, after the `ADDR_PLAYER_ANIM` line in the "Game state." block, add:

```python
# Controller 1 held buttons, byte 2 (A X L R - - - -). kaizosplits buttonsHeld2.
# Read for the R-menu command layer: R (0x10) arms the menu, X (0x40) is a
# command button. The newly-pressed twin ($18, buttonsPress2) is intentionally
# NOT read — the menu detector edge-detects the held byte instead (see
# retroarch/menu_detector.py).
ADDR_CONTROLLER_HELD = 0x17
```

- [ ] **Step 2: Write the failing snapshot test**

In `tests/unit/retroarch/test_snapshot.py`, find `test_read_snapshot_maps_each_address_to_its_field`. Add `0x17` to its `addr_to_value` dict and a new cluster/assert. Add this entry to `addr_to_value` (pick an unused sentinel):

```python
        0x0017: 0xCC,  # controller_held
```

Add to the `clusters` list:

```python
        (0x0017, 1),                    # controller_held
```

Add an assertion at the end of the test:

```python
    assert snap.controller_held == 0xCC
```

- [ ] **Step 3: Run it — verify it fails**

Run: `python -m pytest tests/unit/retroarch/test_snapshot.py -q`
Expected: FAIL — `MemorySnapshot` has no `controller_held` (TypeError or AttributeError).

- [ ] **Step 4: Add the field + read**

In `python/spinlab/retroarch/snapshot.py`, add the field at the END of the `MemorySnapshot` dataclass (trailing, with default — see decision 5):

```python
    cp_entrance: int
    # Controller 1 held buttons, byte 2 (A X L R). Read for the R-menu layer.
    # Defaulted because transition detection never consults it — existing
    # snapshot builders legitimately omit it; only read_snapshot + the menu
    # detector set it.
    controller_held: int = 0
```

In `read_snapshot`, add a 7th lone read next to the other lone bytes and pass it through. After the `cp_entrance = ...` line:

```python
    cp_entrance = client.read_ram(a.ADDR_CP_ENTRANCE, 1)[0]
    controller_held = client.read_ram(a.ADDR_CONTROLLER_HELD, 1)[0]
```

Add to the `MemorySnapshot(...)` return:

```python
        cp_entrance=cp_entrance,
        controller_held=controller_held,
    )
```

Update the docstring: change "Read all 11 SMW state bytes" → "12 SMW state bytes" and "6 ... round-trips" → "7 ... round-trips" with a one-line note that `$17` is a lone read far below the low cluster.

- [ ] **Step 5: Run it — verify it passes**

Run: `python -m pytest tests/unit/retroarch/test_snapshot.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/retroarch/addresses.py python/spinlab/retroarch/snapshot.py tests/unit/retroarch/test_snapshot.py
git commit -m "feat(snapshot): read controller-1 held byte (\$17) for the R-menu layer"
```

---

### Task 2: Protocol events + pause command

**Files:**
- Modify: `python/spinlab/protocol.py`
- Test: `tests/unit/test_protocol_menu.py` (new, tiny)

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_protocol_menu.py`:

```python
"""The R-menu protocol surface: command + armed events, pause command."""
from spinlab.protocol import (
    ControllerCommandEvent,
    ControllerMenuArmedEvent,
    PracticePauseCmd,
)


def test_controller_command_defaults_and_field():
    assert ControllerCommandEvent().command == ""
    assert ControllerCommandEvent(command="pause").command == "pause"


def test_controller_menu_armed_event():
    assert ControllerMenuArmedEvent(armed=True).armed is True
    assert ControllerMenuArmedEvent(armed=False).armed is False


def test_practice_pause_cmd_constructs():
    assert PracticePauseCmd() is not None
```

- [ ] **Step 2: Run it — verify it fails**

Run: `python -m pytest tests/unit/test_protocol_menu.py -q`
Expected: FAIL — ImportError.

- [ ] **Step 3: Add the events + command**

In `python/spinlab/protocol.py`, in the Events section (after `AttemptInvalidatedEvent`, before the HyperPlay events), add:

```python
@dataclass(frozen=True)
class ControllerCommandEvent:
    """An R-menu command dispatched from the controller. command is a key from
    the menu_detector COMMANDS registry (today only "pause")."""
    command: str = ""

@dataclass(frozen=True)
class ControllerMenuArmedEvent:
    """The R-menu arm state changed (R held past threshold / R released).
    Drives the dashboard's 'X — Pause' hint."""
    armed: bool = False
```

In the Commands section (after `PracticeStopCmd`), add:

```python
@dataclass
class PracticePauseCmd:
    """Disarm the backend PracticeTiming without ending the practice loop.
    Sent by PracticeSession.toggle_pause to drop the in-flight attempt; resume
    re-arms by re-sending the stashed PracticeLoadCmd."""
    pass
```

> Note: these events are intentionally NOT added to the `PollerEvent` union — that union types the memory-transition stream that gets `state_path`/`conditions` stamping, which does not apply to menu events (see Task 4).

- [ ] **Step 4: Run it — verify it passes**

Run: `python -m pytest tests/unit/test_protocol_menu.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/protocol.py tests/unit/test_protocol_menu.py
git commit -m "feat(protocol): ControllerCommand/MenuArmed events + PracticePauseCmd"
```

---

### Task 3: ControllerMenuDetector

**Files:**
- Create: `python/spinlab/retroarch/menu_detector.py`
- Test: `tests/unit/retroarch/test_menu_detector.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/retroarch/test_menu_detector.py`:

```python
"""ControllerMenuDetector — R arms a command menu; X dispatches pause."""
from spinlab.protocol import ControllerCommandEvent, ControllerMenuArmedEvent
from spinlab.retroarch.menu_detector import (
    ARM_THRESHOLD_FRAMES,
    BUTTON_R,
    BUTTON_X,
    ControllerMenuDetector,
)
from spinlab.retroarch.snapshot import MemorySnapshot


def _snap(controller_held: int = 0) -> MemorySnapshot:
    return MemorySnapshot(
        game_mode=0, level_num=0, room_num=0, level_start=0, player_anim=0,
        exit_mode=0, io_port=0, fanfare=0, boss_defeat=0, midway=0,
        cp_entrance=0, controller_held=controller_held,
    )


def _run(detector, snaps):
    out = []
    for s in snaps:
        out.extend(detector.step(s))
    return out


def test_r_held_below_threshold_does_not_arm():
    d = ControllerMenuDetector()
    events = _run(d, [_snap(BUTTON_R)] * (ARM_THRESHOLD_FRAMES - 1))
    assert events == []


def test_r_held_past_threshold_arms():
    d = ControllerMenuDetector()
    events = _run(d, [_snap(BUTTON_R)] * ARM_THRESHOLD_FRAMES)
    armed = [e for e in events if isinstance(e, ControllerMenuArmedEvent)]
    assert len(armed) == 1 and armed[0].armed is True


def test_armed_then_x_press_emits_pause():
    d = ControllerMenuDetector()
    # Arm with R, then press X while still holding R (rising edge of X).
    snaps = [_snap(BUTTON_R)] * ARM_THRESHOLD_FRAMES + [_snap(BUTTON_R | BUTTON_X)]
    events = _run(d, snaps)
    cmds = [e for e in events if isinstance(e, ControllerCommandEvent)]
    assert len(cmds) == 1 and cmds[0].command == "pause"


def test_x_does_not_refire_while_held():
    d = ControllerMenuDetector()
    snaps = (
        [_snap(BUTTON_R)] * ARM_THRESHOLD_FRAMES
        + [_snap(BUTTON_R | BUTTON_X)] * 5  # held across frames -> one dispatch
    )
    events = _run(d, snaps)
    cmds = [e for e in events if isinstance(e, ControllerCommandEvent)]
    assert len(cmds) == 1


def test_release_r_disarms():
    d = ControllerMenuDetector()
    snaps = [_snap(BUTTON_R)] * ARM_THRESHOLD_FRAMES + [_snap(0)]
    events = _run(d, snaps)
    armed = [e for e in events if isinstance(e, ControllerMenuArmedEvent)]
    assert [e.armed for e in armed] == [True, False]


def test_lone_x_without_r_does_nothing():
    d = ControllerMenuDetector()
    events = _run(d, [_snap(BUTTON_X)] * ARM_THRESHOLD_FRAMES)
    assert events == []
```

- [ ] **Step 2: Run them — verify they fail**

Run: `python -m pytest tests/unit/retroarch/test_menu_detector.py -q`
Expected: FAIL — ImportError (`menu_detector` doesn't exist).

- [ ] **Step 3: Implement the detector**

Create `python/spinlab/retroarch/menu_detector.py`:

```python
"""ControllerMenuDetector — the R-menu command layer.

A poller-level state machine over the per-frame held-button byte. Holding R
(controller-1 $17, bit 0x10) for ARM_THRESHOLD_FRAMES consecutive frames arms
a command menu. While armed, the rising edge of a command button dispatches
the mapped command. Releasing R disarms.

Single responsibility: it knows nothing about practice/pause — it only turns
controller input into ControllerMenuArmedEvent / ControllerCommandEvent, which
SessionManager routes. The command registry is built to extend (add a button
bit -> command name); X = pause is the only command today.
"""
from __future__ import annotations

from spinlab.protocol import ControllerCommandEvent, ControllerMenuArmedEvent
from spinlab.retroarch.snapshot import MemorySnapshot

# $17 bit layout is A X L R - - - - (kaizosplits buttonsHeld2).
BUTTON_R = 0x10  # bit 4 — arms the menu
BUTTON_X = 0x40  # bit 6 — first command button

# Frames of continuous R-hold before the menu arms. 30 frames ~= 0.5s at 60Hz:
# long enough that an in-play look-ahead R tap never reaches it (so the menu
# can't arm mid-platforming), short enough to feel responsive on a deliberate
# hold. Lives here as a named constant per the no-magic-numbers guideline.
ARM_THRESHOLD_FRAMES = 30

# Command-button bit -> command name. Extend by adding a key. The OR of all
# keys is the mask we track for rising-edge detection.
COMMANDS: dict[int, str] = {BUTTON_X: "pause"}
COMMAND_MASK = 0
for _bit in COMMANDS:
    COMMAND_MASK |= _bit

_MenuEvent = ControllerCommandEvent | ControllerMenuArmedEvent


class ControllerMenuDetector:
    """Per-frame R-menu emitter. Stateful but pure (no IO)."""

    def __init__(self) -> None:
        self._r_held_frames = 0
        self._armed = False
        # Command-button bits seen held last frame (only meaningful while
        # armed) — used to fire on the rising edge instead of every frame.
        self._prev_command_bits = 0

    def reset(self) -> None:
        self._r_held_frames = 0
        self._armed = False
        self._prev_command_bits = 0

    def step(self, snap: MemorySnapshot) -> list[_MenuEvent]:
        events: list[_MenuEvent] = []
        held = snap.controller_held
        r_down = bool(held & BUTTON_R)

        self._r_held_frames = self._r_held_frames + 1 if r_down else 0

        # ARM on reaching the threshold; DISARM the moment R is released.
        if self._r_held_frames >= ARM_THRESHOLD_FRAMES and not self._armed:
            self._armed = True
            events.append(ControllerMenuArmedEvent(armed=True))
        elif not r_down and self._armed:
            self._armed = False
            self._prev_command_bits = 0
            events.append(ControllerMenuArmedEvent(armed=False))

        # DISPATCH: while armed, each command button fires on its rising edge.
        if self._armed:
            for bit, command in COMMANDS.items():
                if (held & bit) and not (self._prev_command_bits & bit):
                    events.append(ControllerCommandEvent(command=command))
            self._prev_command_bits = held & COMMAND_MASK

        return events
```

- [ ] **Step 4: Run them — verify they pass**

Run: `python -m pytest tests/unit/retroarch/test_menu_detector.py -q`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/retroarch/menu_detector.py tests/unit/retroarch/test_menu_detector.py
git commit -m "feat(menu): ControllerMenuDetector — R arms, X dispatches pause"
```

---

### Task 4: Poller runs the menu detector + forwards its events

**Files:**
- Modify: `python/spinlab/retroarch/poller.py`
- Test: `tests/unit/retroarch/test_poller.py`

- [ ] **Step 1: Write the failing test**

In `tests/unit/retroarch/test_poller.py`, add (the `_snap`, `_make_snapshots`, `_FakeClient` helpers already exist at the top of the file):

```python
async def test_poller_forwards_menu_events():
    """The poller steps the injected menu detector and forwards its events."""
    from spinlab.protocol import ControllerCommandEvent
    from spinlab.retroarch.poller import Poller, PollerDeps

    class _StubMenu:
        def __init__(self):
            self.calls = 0
        def reset(self):
            pass
        def step(self, _snap):
            self.calls += 1
            return [ControllerCommandEvent(command="pause")] if self.calls == 1 else []

    snapshots = iter([_snap(), _snap(), _snap()])
    received: list = []
    deps = PollerDeps(
        client=_FakeClient(),
        read_snapshot=_make_snapshots(snapshots),
        on_event=received.append,
    )
    poller = Poller(deps, period_sec=0.001, menu=_StubMenu())
    task = asyncio.create_task(poller.run())
    await asyncio.sleep(0.05)
    poller.stop()
    await task

    cmds = [e for e in received if isinstance(e, ControllerCommandEvent)]
    assert len(cmds) == 1 and cmds[0].command == "pause"
```

(Add `controller_held=...` is unnecessary — `_snap` defaults it. The stub returns the event independent of snapshot content.)

- [ ] **Step 2: Run it — verify it fails**

Run: `python -m pytest tests/unit/retroarch/test_poller.py::test_poller_forwards_menu_events -q`
Expected: FAIL — `Poller.__init__` has no `menu` parameter.

- [ ] **Step 3: Wire the menu detector into the poller**

In `python/spinlab/retroarch/poller.py`:

Add the import near the other detector imports:

```python
from spinlab.retroarch.menu_detector import ControllerMenuDetector
```

Add the constructor param + field. Change the `__init__` signature:

```python
    def __init__(
        self,
        deps: PollerDeps,
        period_sec: float = DEFAULT_PERIOD_SEC,
        detector: TransitionDetector | None = None,
        cold_fill: ColdFillSpawnDetector | None = None,
        menu: ControllerMenuDetector | None = None,
    ) -> None:
```

After the `self._cold_fill = ...` line:

```python
        self._menu = menu if menu is not None else ControllerMenuDetector()
```

Add a fault flag next to `self._cold_fill_failing`:

```python
        self._menu_failing: bool = False
```

In `run()`, after the cold-fill block (the `if cf_event is not None:` block, just before the trailing `await asyncio.sleep(self._period)`), add a menu block. Menu events bypass the `_stamp_state_path` / `_stamp_conditions` pipeline — they have neither field, and `_stamp_conditions`'s unconditional `dataclasses.replace(ev, conditions=...)` would raise on them:

```python
            try:
                menu_events = list(self._menu.step(snap))
            except Exception as exc:
                if not self._menu_failing:
                    log.error(logger, "menu.step raised", exc=exc)
                    self._menu_failing = True
                menu_events = []
            else:
                if self._menu_failing:
                    log.info(logger, "menu.step recovered")
                    self._menu_failing = False
            for mev in menu_events:
                # Infrastructure events — no state_path/conditions stamping.
                try:
                    self._deps.on_event(mev)
                except Exception as exc:
                    log.error(
                        logger, "poller event handler raised",
                        exc=exc, event_type=type(mev).__name__,
                    )
```

> Note: on a state-load resync tick the poller `continue`s before any `.step()`, so the menu detector simply skips that frame — correct, since R-hold is live input independent of savestate loads.

- [ ] **Step 4: Run it — verify it passes**

Run: `python -m pytest tests/unit/retroarch/test_poller.py -q`
Expected: PASS (existing poller tests + the new one).

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/retroarch/poller.py tests/unit/retroarch/test_poller.py
git commit -m "feat(poller): step ControllerMenuDetector and forward its events"
```

---

### Task 5: Orchestrator handles PracticePauseCmd (disarm)

**Files:**
- Modify: `python/spinlab/retroarch/orchestrator.py`
- Test: `tests/unit/retroarch/test_orchestrator.py` (find it; if absent, add a focused test file)

- [ ] **Step 1: Locate the orchestrator test file**

Run: `git ls-files tests | grep -i orchestrator`
Use the file that constructs a `RetroArchOrchestrator` with stub components (the `_practice_timing` is duck-typed). If no orchestrator unit test exists, create `tests/unit/retroarch/test_orchestrator_pause.py` and build the orchestrator with `MagicMock()` components.

- [ ] **Step 2: Write the failing test**

Add (adapt construction to the existing test's helper; this standalone version uses mocks):

```python
import pytest
from unittest.mock import MagicMock
from spinlab.protocol import PracticePauseCmd
from spinlab.retroarch.orchestrator import RetroArchOrchestrator


def _orch():
    return RetroArchOrchestrator(
        raclient=MagicMock(), poller=MagicMock(), conditions=MagicMock(),
        practice_timing=MagicMock(), hyper_play_timing=MagicMock(),
        state_paths=MagicMock(), movies=MagicMock(),
    )


@pytest.mark.asyncio
async def test_practice_pause_cmd_disarms_timing():
    orch = _orch()
    await orch.send_command(PracticePauseCmd())
    orch._practice_timing.disarm.assert_called_once()
```

- [ ] **Step 3: Run it — verify it fails**

Run: `python -m pytest tests/unit/retroarch/test_orchestrator_pause.py -q`
Expected: FAIL — `PracticePauseCmd` is an unknown cmd type (logged + ignored), so `disarm` is never called.

- [ ] **Step 4: Add the handler + dispatch entry**

In `python/spinlab/retroarch/orchestrator.py`:

Add `PracticePauseCmd` to the protocol import block (alongside `PracticeStopCmd`).

Add to `self._dispatch` (after the `PracticeStopCmd:` entry):

```python
            PracticePauseCmd: self._on_practice_pause,
```

Add the handler next to `_on_practice_stop`:

```python
    async def _on_practice_pause(self, cmd: PracticePauseCmd) -> None:
        # Pause drops the in-flight attempt by disarming timing; resume re-arms
        # via a fresh PracticeLoadCmd from PracticeSession.toggle_pause.
        self._practice_timing.disarm()
```

- [ ] **Step 5: Run it — verify it passes**

Run: `python -m pytest tests/unit/retroarch/test_orchestrator_pause.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/retroarch/orchestrator.py tests/unit/retroarch/test_orchestrator_pause.py
git commit -m "feat(orchestrator): PracticePauseCmd disarms PracticeTiming"
```

---

### Task 6: PracticeSession pause toggle

**Files:**
- Modify: `python/spinlab/practice.py`
- Test: `tests/unit/test_practice.py`

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_practice.py`, add:

```python
class TestTogglePause:
    @pytest.mark.asyncio
    async def test_pause_disarms_then_resume_reloads_same_segment(self, practice_db):
        from spinlab.protocol import PracticeLoadCmd, PracticePauseCmd
        emu = AsyncMock()
        emu.is_connected = True
        emu.send_command = AsyncMock()
        ps = PracticeSession(emu=emu, db=practice_db, game_id="g",
                             scheduler=Scheduler(practice_db, "g"))
        ps.is_running = True
        # Simulate an attempt in flight (run_one sets these before awaiting).
        load_cmd = PracticeLoadCmd(id="seg1", state_path="s.state", end_type="goal")
        ps._current_state_path = "s.state"
        ps._current_load_cmd = load_cmd

        # PLAYING -> PAUSE
        await ps.toggle_pause()
        assert ps.paused is True
        assert ps.paused_at_epoch is not None
        sent = [c.args[0] for c in emu.send_command.call_args_list]
        assert any(isinstance(c, PracticePauseCmd) for c in sent)

        # PAUSED -> RESUME re-sends the SAME load cmd and clears paused.
        await ps.toggle_pause()
        assert ps.paused is False
        assert ps.paused_at_epoch is None
        assert ps.pause_offset_sec >= 0.0
        sent = [c.args[0] for c in emu.send_command.call_args_list]
        assert sent[-1] is load_cmd

    @pytest.mark.asyncio
    async def test_pause_noop_when_not_in_attempt(self, practice_db):
        emu = AsyncMock(); emu.is_connected = True; emu.send_command = AsyncMock()
        ps = PracticeSession(emu=emu, db=practice_db, game_id="g",
                             scheduler=Scheduler(practice_db, "g"))
        ps.is_running = True
        ps._current_state_path = None  # between attempts
        await ps.toggle_pause()
        assert ps.paused is False
        emu.send_command.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_death_ignored_while_paused(self, practice_db):
        emu = AsyncMock(); emu.is_connected = True
        emu.load_state = AsyncMock(); emu.send_command = AsyncMock()
        ps = PracticeSession(emu=emu, db=practice_db, game_id="g",
                             scheduler=Scheduler(practice_db, "g"))
        ps._current_state_path = "s.state"
        ps.paused = True
        await ps.handle_death()
        emu.load_state.assert_not_called()
```

- [ ] **Step 2: Run them — verify they fail**

Run: `python -m pytest tests/unit/test_practice.py::TestTogglePause -q`
Expected: FAIL — no `toggle_pause` / `paused` attributes.

- [ ] **Step 3: Implement pause on PracticeSession**

In `python/spinlab/practice.py`:

Add `import time` at the top (next to `import os`).

In `__init__`, after the `self._current_state_path: str | None = None` block, add:

```python
        # --- Practice Pause (R+X) state -------------------------------------
        # paused: True while the session clock is frozen and the in-flight
        # attempt has been dropped (backend timing disarmed). paused_at_epoch
        # marks the current pause's start (wall-clock epoch seconds);
        # pause_offset_sec accumulates completed paused spans. The route bar
        # subtracts both to freeze elapsed + savings/hr. _current_load_cmd is
        # the segment's PracticeLoadCmd, re-sent on resume to reload + re-arm.
        self.paused: bool = False
        self.paused_at_epoch: float | None = None
        self.pause_offset_sec: float = 0.0
        self._current_load_cmd: "PracticeLoadCmd | None" = None
```

Import `PracticePauseCmd` into the protocol import block at the top:

```python
from .protocol import (
    AttemptResultEvent,
    EventAttemptEmission,
    PracticeLoadCmd,
    PracticePauseCmd,
    PracticeStopCmd,
)
```

In `run_one`, stash the load cmd. Replace the `await self.emu.send_command(PracticeLoadCmd(...))` call with a stashed local:

```python
        self._current_load_cmd = PracticeLoadCmd(
            id=cmd.id,
            state_path=cmd.state_path,
            description=cmd.description,
            end_type=cmd.end_type,
            expected_time_ms=cmd.expected_time_ms,
            auto_advance_delay_ms=cmd.auto_advance_delay_ms,
            death_penalty_ms=cmd.death_penalty_ms,
        )
        await self.emu.send_command(self._current_load_cmd)
```

Gate `handle_death` — add at the very top of the method, before the `path = ...` line:

```python
        if self.paused:
            logger.info("practice: death ignored — paused")
            return
```

Add the toggle method (after `handle_level_exit_abort`):

```python
    async def toggle_pause(self) -> None:
        """R-menu pause toggle. Pause drops the in-flight attempt and freezes
        the session clock; resume reloads the same segment fresh.

        Only meaningful mid-attempt: _current_state_path is non-None exactly
        while an attempt is armed (set in run_one, cleared in receive_result).
        """
        if not self.is_running or self._current_state_path is None:
            logger.info("practice: pause ignored — no attempt in flight")
            return
        if not self.paused:
            # PLAYING -> PAUSE: disarm backend timing (drops the attempt), freeze
            # the clock. The game keeps running; handle_death no-ops while paused.
            self.paused = True
            self.paused_at_epoch = time.time()
            await self.emu.send_command(PracticePauseCmd())
            logger.info("practice: paused (segment=%s)", self.current_segment_id)
        else:
            # PAUSED -> RESUME: fold the paused span into the offset, then reload
            # the SAME segment fresh (re-arms a new attempt via _on_practice_load).
            if self.paused_at_epoch is not None:
                self.pause_offset_sec += time.time() - self.paused_at_epoch
            self.paused = False
            self.paused_at_epoch = None
            if self._current_load_cmd is not None:
                await self.emu.send_command(self._current_load_cmd)
            logger.info("practice: resumed (segment=%s)", self.current_segment_id)
```

> The `PracticeLoadCmd` forward reference in `__init__`'s annotation resolves because `PracticeLoadCmd` is imported at module top.

- [ ] **Step 4: Run them — verify they pass**

Run: `python -m pytest tests/unit/test_practice.py -q`
Expected: PASS (existing practice tests + the 3 new ones).

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/practice.py tests/unit/test_practice.py
git commit -m "feat(practice): toggle_pause — disarm + freeze clock + reload on resume"
```

---

### Task 7: SessionManager menu/command routing + `menu_armed` state

**Files:**
- Modify: `python/spinlab/system_state.py`
- Modify: `python/spinlab/session_manager.py`
- Test: `tests/unit/test_session_manager.py`

- [ ] **Step 1: Write the failing test**

In `tests/unit/test_session_manager.py`, add (mirror the file's existing SessionManager construction; it builds one with a `FakeEmuBackend` + `Database`):

```python
class TestRMenuRouting:
    @pytest.mark.asyncio
    async def test_menu_armed_event_sets_state(self, <existing session fixture>):
        from spinlab.protocol import ControllerMenuArmedEvent
        sm = <session manager>
        await sm.route_event(ControllerMenuArmedEvent(armed=True))
        assert sm.state.menu_armed is True
        await sm.route_event(ControllerMenuArmedEvent(armed=False))
        assert sm.state.menu_armed is False

    @pytest.mark.asyncio
    async def test_pause_command_calls_toggle_in_practice(self, <existing session fixture>):
        from unittest.mock import AsyncMock
        from spinlab.models import Mode
        from spinlab.protocol import ControllerCommandEvent
        sm = <session manager>
        sm.mode = Mode.PRACTICE
        sm.practice_session = AsyncMock()
        await sm.route_event(ControllerCommandEvent(command="pause"))
        sm.practice_session.toggle_pause.assert_awaited_once()
```

> Replace `<existing session fixture>` / `<session manager>` with the construction pattern already in this test file. Keep the test minimal — it only checks routing.

- [ ] **Step 2: Run it — verify it fails**

Run: `python -m pytest tests/unit/test_session_manager.py::TestRMenuRouting -q`
Expected: FAIL — no `menu_armed` on state; no handlers registered (route_event logs "No handler").

- [ ] **Step 3: Add the state field + handlers**

In `python/spinlab/system_state.py`, add to `SystemState`:

```python
    # R-menu armed (R held past threshold). Drives the 'X — Pause' hint; pushed
    # to the route bar via the live-summary payload. Mode-agnostic at the
    # detector; the frontend only renders it during practice.
    menu_armed: bool = False
```

In `python/spinlab/session_manager.py`:

Add to the protocol import block:

```python
    ControllerCommandEvent,
    ControllerMenuArmedEvent,
```

Register in `self._event_handlers` (anywhere in the dict, e.g. after `AttemptInvalidatedEvent`):

```python
            ControllerCommandEvent: self._handle_controller_command,
            ControllerMenuArmedEvent: self._handle_controller_menu_armed,
```

Add the handlers (next to `_handle_attempt_invalidated`):

```python
    async def _handle_controller_menu_armed(self, event: ControllerMenuArmedEvent) -> None:
        self.state.menu_armed = event.armed
        await self._notify_sse()

    async def _handle_controller_command(self, event: ControllerCommandEvent) -> None:
        if event.command != "pause":
            logger.warning("unknown controller command: %r", event.command)
            return
        # Practice-scoped: the input layer is mode-agnostic but pause only
        # applies to a practice attempt (spec — practice mode only for now).
        if self.mode == Mode.PRACTICE and self.practice_session:
            await self.practice_session.toggle_pause()
        await self._notify_sse()
```

- [ ] **Step 4: Run it — verify it passes**

Run: `python -m pytest tests/unit/test_session_manager.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/system_state.py python/spinlab/session_manager.py tests/unit/test_session_manager.py
git commit -m "feat(session): route R-menu armed/command events; pause toggles practice"
```

---

### Task 8: RouteSummary exposes pause + menu-armed fields

**Files:**
- Modify: `python/spinlab/api_schemas.py`
- Modify: `python/spinlab/routes/model.py`
- Test: find the existing live-summary route test (`git ls-files tests | grep -i model`) or add to it.

- [ ] **Step 1: Write the failing test**

Locate the live-summary route test. If `tests/unit/test_routes_model.py` (or similar) exists with a `get_route_summary` test, add an assertion that the three new keys are present and default sanely when no session is active:

```python
def test_live_summary_includes_pause_fields(<client/session fixtures>):
    resp = <GET /api/games/{gid}/live-summary>
    body = resp.json()
    assert "menu_armed" in body and body["menu_armed"] is False
    assert "session_paused_at" in body and body["session_paused_at"] is None
    assert "session_pause_offset_sec" in body and body["session_pause_offset_sec"] == 0.0
```

> If no such route test exists, add a thin one using the app's `TestClient` fixture (pattern: `git ls-files tests | grep -i route`). Keep it to the contract check above.

- [ ] **Step 2: Run it — verify it fails**

Run: `python -m pytest <the test> -q`
Expected: FAIL — keys absent from the response.

- [ ] **Step 3: Add the schema fields**

In `python/spinlab/api_schemas.py`, in `RouteSummaryResponse` (after `session_ended_at`), add:

```python
    # R-menu / Practice Pause overlay.
    menu_armed: bool = False               # R held past threshold -> show 'X — Pause' hint
    session_paused_at: float | None = None # epoch seconds the current pause began; None = not paused
    session_pause_offset_sec: float = 0.0  # accumulated completed-pause seconds; route bar subtracts it
```

- [ ] **Step 4: Populate them in the endpoint**

In `python/spinlab/routes/model.py`, in `get_route_summary`, before the `return {`, add:

```python
    ps = session.practice_session
    session_paused_at = ps.paused_at_epoch if (ps is not None and ps.paused) else None
    session_pause_offset_sec = ps.pause_offset_sec if ps is not None else 0.0
```

Add to the returned dict (after `"floor_series": floor_series,`):

```python
        "menu_armed": session.state.menu_armed,
        "session_paused_at": session_paused_at,
        "session_pause_offset_sec": session_pause_offset_sec,
```

- [ ] **Step 5: Run it — verify it passes**

Run: `python -m pytest <the test> -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/api_schemas.py python/spinlab/routes/model.py tests/<the test file>
git commit -m "feat(api): live-summary exposes menu_armed + pause clock fields"
```

---

### Task 9: Frontend — freeze the clock, PAUSED badge, R-menu hint

**Files:**
- Modify: `frontend/src/route-bar.ts`
- Modify: the live-view CSS (find with `git ls-files frontend | grep -iE "css"`; likely `frontend/src/styles.css`)
- Test: `frontend/src/route-bar.test.ts` (find existing; `git ls-files frontend | grep route-bar`)

- [ ] **Step 1: Regenerate types so `RouteSummary` carries the new fields**

Run: `cd frontend && npm run gen-types`
Confirm `frontend/src/api-types.ts` now lists `menu_armed`, `session_paused_at`, `session_pause_offset_sec` under `RouteSummaryResponse`.

- [ ] **Step 2: Write the failing vitest test**

In the route-bar test file, add (adapt the existing `RouteSummary` literal builder used there — fill required fields as the existing tests do):

```typescript
import { describe, it, expect } from "vitest";
import { renderRouteBar } from "./route-bar";
import type { RouteSummary } from "./types";

function baseSummary(overrides: Partial<RouteSummary> = {}): RouteSummary {
  return {
    game_id: "g", exp_run_ms: 1000, exp_deaths: 0, n_estimable: 1, n_skipped: 0,
    session_started_at: 1000, session_ended_at: null,
    exp_run_diff_ms: null, exp_deaths_diff: null, practice_saved_ms: 5000,
    floor_improvement_ms: 0, run_series: [], baseline_exp_run_ms: null,
    floor_total_ms: null, floor_series: [],
    menu_armed: false, session_paused_at: null, session_pause_offset_sec: 0,
    ...overrides,
  } as RouteSummary;
}

describe("route bar pause/menu", () => {
  it("freezes elapsed at the pause-start while paused", () => {
    const host = document.createElement("div");
    // started_at=1000, paused_at=1100 -> elapsed pinned to 100s regardless of now.
    renderRouteBar(host, {
      title: "t", gameId: "g", nowSeconds: 9999,
      routeSummary: baseSummary({ session_paused_at: 1100 }),
    });
    expect(host.querySelector(".rb-paused")).not.toBeNull();
    expect(host.innerHTML).toContain("0:01:40"); // 100s = 1:40
  });

  it("subtracts the pause offset when running", () => {
    const host = document.createElement("div");
    // now=1300, started=1000, offset=100 -> elapsed = 300-100 = 200s = 3:20.
    renderRouteBar(host, {
      title: "t", gameId: "g", nowSeconds: 1300,
      routeSummary: baseSummary({ session_pause_offset_sec: 100 }),
    });
    expect(host.innerHTML).toContain("0:03:20");
  });

  it("shows the R-menu hint when armed", () => {
    const host = document.createElement("div");
    renderRouteBar(host, {
      title: "t", gameId: "g", nowSeconds: 1000,
      routeSummary: baseSummary({ menu_armed: true }),
    });
    expect(host.querySelector(".rb-menu-hint")).not.toBeNull();
  });
});
```

- [ ] **Step 3: Run it — verify it fails**

Run: `cd frontend && npm test -- route-bar`
Expected: FAIL — no `.rb-paused` / offset math / `.rb-menu-hint`.

- [ ] **Step 4: Implement in `route-bar.ts`**

In `renderRouteBar`, replace the elapsed-seconds computation:

```typescript
  const sessionStartedAt = rs.session_started_at ?? null;
  const sessionActive = sessionStartedAt != null;
  // Freeze the clock while paused: pin "now" to the pause-start and subtract
  // the accumulated paused span so elapsed (and savings/hr, which divides by
  // it) stop advancing. Resume clears session_paused_at and grows the offset.
  const pauseOffset = rs.session_pause_offset_sec ?? 0;
  const pausedAt = rs.session_paused_at ?? null;
  const effectiveNow = pausedAt != null ? pausedAt : data.nowSeconds;
  const elapsedSec = sessionActive
    ? Math.max(0, effectiveNow - sessionStartedAt - pauseOffset)
    : 0;
```

Add badge + hint locals near `frozenBadge`:

```typescript
  const pausedBadge = pausedAt != null
    ? `<span class="rb-paused">PAUSED</span>`
    : "";
  const menuHint = rs.menu_armed
    ? `<div class="rb-menu-hint">R menu — X: Pause</div>`
    : "";
```

Render them: change the title line to include `pausedBadge`, and add `menuHint` into `rb-left`:

```typescript
  host.innerHTML = `
    <div class="rb-root">
      <div class="rb-left">
        <div class="rb-title">${escapeHtml(data.title)}${frozenBadge}${pausedBadge}</div>
        ${savedBlock}
        ${menuHint}
        ${skippedBlock}
      </div>
      <div class="rb-stats">${stacks.join("")}</div>
    </div>
  `;
```

- [ ] **Step 5: Add CSS**

In the live-view CSS file, add:

```css
.rb-paused { margin-left: 0.5rem; color: var(--warn, #e0a000); font-weight: 600; }
.rb-menu-hint { font-size: 0.85em; color: var(--dim, #888); margin-top: 0.15rem; }
```

(Use the variables/colors the file already uses for `.rb-frozen` / `.dim`.)

- [ ] **Step 6: Run it + typecheck**

Run: `cd frontend && npm test -- route-bar && npm run typecheck`
Expected: PASS, no type errors.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/route-bar.ts frontend/src/api-types.ts frontend/src/*.css <route-bar test>
git commit -m "feat(frontend): route bar freezes clock when paused + R-menu hint"
```

---

### Task 10: Emulator confirmation — poke `$17` on real RA

**Files:**
- Modify: `tests/integration/addresses.py`
- Modify: `tests/integration/ra_poke_engine.py`
- Create: `tests/integration/scenarios/menu_pause.poke`
- Test: `tests/integration/test_transitions.py` (or a new `test_menu_commands.py`)

- [ ] **Step 1: Map the address for the poke parser**

In `tests/integration/addresses.py`, add to `ADDR_MAP`:

```python
    "controller_held": _a.ADDR_CONTROLLER_HELD,
```

- [ ] **Step 2: Run the menu detector inside the poke engine**

In `tests/integration/ra_poke_engine.py`:

Add the import:

```python
from spinlab.retroarch.menu_detector import ControllerMenuDetector
```

In `run_scenario`, construct it next to the transition detector:

```python
        detector = TransitionDetector()
        menu = ControllerMenuDetector()
```

In the per-frame loop, after `new_events = list(detector.step(...))`, also step the menu detector and merge so both feed `events` and the quiescence clock:

```python
            new_events = list(detector.step(snap, frame * FRAME_PERIOD_MS))
            new_events.extend(menu.step(snap))
            events.extend(new_events)
            if new_events:
                frame_of_last_event = frame
```

Update the module docstring's step-2 line to mention the menu detector. (Existing transition scenarios poke `controller_held=0` implicitly via the ADDR_MAP zeroing, so the menu detector stays silent for them — their event lists are unchanged.)

- [ ] **Step 3: Create the scenario**

Create `tests/integration/scenarios/menu_pause.poke`:

```
# menu_pause — hold R ($17 bit 0x10) to arm the menu, then press X (0x40)
# to dispatch the 'pause' command. 0x10 from frame 1 arms at frame 30
# (ARM_THRESHOLD_FRAMES); 0x50 at frame 35 is the X rising edge.
settle: 60

1: controller_held=0x10
35: controller_held=0x50
```

- [ ] **Step 4: Write the failing emulator test**

In `tests/integration/test_transitions.py` (it already has `pytestmark = [pytest.mark.emulator, ...]` and the `run_scenario` fixture), add:

```python
async def test_r_menu_pause_command(run_scenario):
    """Hold R, press X on real RA -> one ControllerCommandEvent('pause').

    This is the live confirmation of the $17 address + bit layout. If R-arm
    never fires here, $17 is not the held A X L R byte — switch
    ADDR_CONTROLLER_HELD to the correct held byte and re-run.
    """
    from spinlab.protocol import ControllerCommandEvent, ControllerMenuArmedEvent
    events = await run_scenario("menu_pause.poke")
    armed = [e for e in events if isinstance(e, ControllerMenuArmedEvent) and e.armed]
    assert len(armed) >= 1, f"menu never armed: {events}"
    cmds = [e for e in events if isinstance(e, ControllerCommandEvent)]
    assert len(cmds) == 1, f"expected 1 pause command, got {cmds}"
    assert cmds[0].command == "pause"
```

- [ ] **Step 5: Run it — verify it passes (requires RA)**

Run: `python -m pytest tests/integration/test_transitions.py::test_r_menu_pause_command -m emulator -q`
Expected: PASS. A `SKIPPED` here is a FAILURE (per CLAUDE.md) — the harness self-launches RA; if it skips, surface why before continuing.

- [ ] **Step 6: Commit**

```bash
git add tests/integration/addresses.py tests/integration/ra_poke_engine.py tests/integration/scenarios/menu_pause.poke tests/integration/test_transitions.py
git commit -m "test(emulator): confirm \$17 R-menu arm + pause dispatch on real RA"
```

---

### Task 11: Full verification gate

- [ ] **Step 1: Fast suite + statics**

```bash
cd frontend && npm run build && cd ..
python -m pytest -m "not emulator" -q
npx pyright python/spinlab/retroarch/menu_detector.py python/spinlab/practice.py python/spinlab/session_manager.py python/spinlab/routes/model.py
ruff check python/spinlab/retroarch/menu_detector.py python/spinlab/practice.py python/spinlab/session_manager.py
cd frontend && npm test && npm run typecheck && cd ..
```
Expected: all green. Do not introduce new pyright/ruff errors in the touched files.

- [ ] **Step 2: Full suite (incl. emulator) — the merge gate**

```bash
python -m pytest
```
Expected: all pass, **zero skips** in the emulator block. Per CLAUDE.md, `SKIPPED` counts as a failure — if the emulator tests skip, surface the launch failure rather than treating it as green.

- [ ] **Step 3: Manual smoke (RA running, dashboard up)**

Hold R for ~0.5s during a practice attempt → the route bar shows "R menu — X: Pause". Press X → card shows PAUSED, elapsed + savings/hr stop ticking; walk Mario into a pit → no reload, nothing recorded. Hold R + press X again → same segment reloads fresh, clock resumes from where it froze.

- [ ] **Step 4: Commit any smoke-fix follow-ups, then hand off to finishing-a-development-branch.**

---

## Self-review notes (checked against the spec)

- **R-menu input layer** (snapshot `$17`, poller-level detector, ARM/DISPATCH/DISARM, `COMMANDS` registry, named constants) → Tasks 1, 3, 4. `$18` deferred per decision 2.
- **Pause behavior** (disarm/IDLE drop, freeze clock via offset, reload-same on resume, emulator not paused, practice-only) → Tasks 5, 6, 7. Leaving pre-pause rows per decision 3.
- **UI feedback** (menu-armed hint; PAUSED card with frozen timer + savings/hr) → Tasks 8, 9, on `RouteSummary` per decision 4.
- **Testing** (menu-detector unit, pause-handling unit, RA poke `$17` confirmation, full gate) → Tasks 3, 6, 10, 11.
- **Out of scope** (other commands, `$15`/`$16`/`$18`, HyperPlay pause, RA pause, freeze-and-continue) → not implemented; `COMMANDS` + the trailing snapshot field leave the extension seam open.
