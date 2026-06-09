# R-menu Phase 2 — Command Dispatch + Toggle Practice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the first mode-spanning R-menu verb — **R+Y = Toggle Practice** (start practice from Idle, stop it from Practice) — and restructure SessionManager's command handler into a small per-command dispatch with each command's own mode rule.

**Architecture:** Y is registered in the (byte,bit) `COMMANDS` map on the `$15` byte (Phase 1 made this possible). SessionManager's `_handle_controller_command` grows from a single `pause` branch into per-command dispatch; `toggle_practice` calls the existing `start_practice`/`stop_practice` and swallows `ActionError` so a controller chord can never surface a 500 or crash the event loop. This is also the first **real-RA `$15` confirmation** — an emulator poke scenario holds R and taps Y.

**Tech Stack:** Python 3.11 (pure detector, async SessionManager), pytest (unit + `emulator` marker via the RA poke harness).

**Spec:** `docs/superpowers/specs/2026-06-09-r-menu-vocabulary-expansion-design.md` (Phase 2). Builds on Phase 1 (`1d34abe`).

## Decisions locked before writing

1. **Y = `$15` bit `0x40`** (kaizosplits buttonsHeld1: `B Y Select Start ...`, Y is bit 6). Same numeric value as X (`0x40`) but a different byte, so `(HELD1, 0x40)` and `(HELD2, 0x40)` are distinct registry keys — no collision.
2. **`toggle_practice` reuses `start_practice`/`stop_practice` verbatim** and catches the `ActionError` base — if practice can't start (pending draft, not connected, snapshot failure) the chord silently no-ops + logs, rather than raising. (The keyboard path surfaces those as HTTP errors; the controller path just does nothing.)
3. **Mode rule:** Idle → start, Practice → stop, every other mode → ignore. Lives in the handler, not the detector.
4. **Emulator:** add `controller_held_1` to the integration `ADDR_MAP` (so the poke engine zeroes `$15` per scenario — prevents a stale `$15` from firing a phantom `toggle_practice` in other scenarios) and a `menu_toggle.poke` scenario. The poke engine already steps the menu detector, and `toggle_practice` is now in the default `COMMANDS`, so no poke-engine change is needed.

## File map

| File | Change |
|---|---|
| `python/spinlab/retroarch/menu_detector.py` | + `BUTTON_Y`; register `(HELD1, BUTTON_Y): "toggle_practice"` |
| `tests/unit/retroarch/test_menu_detector.py` | default-registry Y → toggle_practice test |
| `python/spinlab/session_manager.py` | per-command dispatch + `_toggle_practice_from_menu` |
| `tests/unit/test_session_manager.py` | toggle dispatch tests (idle/practice/other/error) |
| `tests/integration/addresses.py` | + `"controller_held_1"` entry |
| `tests/integration/scenarios/menu_toggle.poke` | **new** scenario |
| `tests/integration/test_transitions.py` | emulator test for the toggle command |

---

### Task 1: Register Toggle Practice (R+Y) in the detector

**Files:**
- Modify: `python/spinlab/retroarch/menu_detector.py`
- Test: `tests/unit/retroarch/test_menu_detector.py`

- [ ] **Step 1: Write the failing test**

In `tests/unit/retroarch/test_menu_detector.py`, append (the `_snap`, `_run`, `_cmds` helpers and `BUTTON_R`/`HELD1`/`HELD2` imports already exist; add `BUTTON_Y` to the import block):

```python
def test_y_after_r_dispatches_toggle_practice_default_registry():
    """R held, then Y pressed -> toggle_practice (Y is a real $15 command now)."""
    d = ControllerMenuDetector()
    events = _run(d, [
        _snap(controller_held=BUTTON_R),                         # R down -> menu open
        _snap(controller_held=BUTTON_R, controller_held_1=BUTTON_Y),  # Y pressed after
    ])
    cmds = _cmds(events)
    assert len(cmds) == 1 and cmds[0].command == "toggle_practice"
```

Add `BUTTON_Y` to the `from spinlab.retroarch.menu_detector import (...)` block at the top of the file.

- [ ] **Step 2: Run it — verify it fails**

Run: `python -m pytest tests/unit/retroarch/test_menu_detector.py::test_y_after_r_dispatches_toggle_practice_default_registry -q`
Expected: FAIL — `BUTTON_Y` import error (and no toggle_practice in the default registry).

- [ ] **Step 3: Add the constant + registry entry**

In `python/spinlab/retroarch/menu_detector.py`, add the `BUTTON_Y` constant after `BUTTON_X`:

```python
BUTTON_X = 0x40  # $17 — pause
BUTTON_Y = 0x40  # $15 — toggle practice (same bit value as X, different byte)
```

Add the toggle-practice entry to `COMMANDS`:

```python
COMMANDS: dict[ButtonKey, str] = {
    (HELD2, BUTTON_X): "pause",
    (HELD1, BUTTON_Y): "toggle_practice",
}
```

Update the module docstring's "X = pause is the only command today." sentence to: "X = pause and Y = toggle_practice are the commands today."

- [ ] **Step 4: Run it — verify it passes**

Run: `python -m pytest tests/unit/retroarch/test_menu_detector.py -q`
Expected: PASS (all existing + the new test).

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/retroarch/menu_detector.py tests/unit/retroarch/test_menu_detector.py
git commit -m "feat(menu): register R+Y = toggle_practice (Y on the 15 byte)"
```

---

### Task 2: SessionManager per-command dispatch + Toggle Practice

**Files:**
- Modify: `python/spinlab/session_manager.py`
- Test: `tests/unit/test_session_manager.py`

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_session_manager.py`, append (the file has `make_sm(db, emu, **kwargs)`, `db`/`emu` fixtures, `Mode`, `pytest`, `MagicMock` already imported):

```python
class TestTogglePracticeCommand:
    @pytest.mark.asyncio
    async def test_toggle_from_idle_starts_practice(self, db, emu):
        from unittest.mock import AsyncMock
        from spinlab.protocol import ControllerCommandEvent
        sm = make_sm(db, emu)
        sm.mode = Mode.IDLE
        sm.start_practice = AsyncMock()
        sm.stop_practice = AsyncMock()
        await sm.route_event(ControllerCommandEvent(command="toggle_practice"))
        sm.start_practice.assert_awaited_once()
        sm.stop_practice.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_toggle_from_practice_stops_practice(self, db, emu):
        from unittest.mock import AsyncMock
        from spinlab.protocol import ControllerCommandEvent
        sm = make_sm(db, emu)
        sm.mode = Mode.PRACTICE
        sm.start_practice = AsyncMock()
        sm.stop_practice = AsyncMock()
        await sm.route_event(ControllerCommandEvent(command="toggle_practice"))
        sm.stop_practice.assert_awaited_once()
        sm.start_practice.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_toggle_ignored_outside_idle_practice(self, db, emu):
        from unittest.mock import AsyncMock
        from spinlab.protocol import ControllerCommandEvent
        sm = make_sm(db, emu)
        sm.mode = Mode.REFERENCE
        sm.start_practice = AsyncMock()
        sm.stop_practice = AsyncMock()
        await sm.route_event(ControllerCommandEvent(command="toggle_practice"))
        sm.start_practice.assert_not_awaited()
        sm.stop_practice.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_toggle_swallows_action_error(self, db, emu):
        from unittest.mock import AsyncMock
        from spinlab.errors import DraftPendingError
        from spinlab.protocol import ControllerCommandEvent
        sm = make_sm(db, emu)
        sm.mode = Mode.IDLE
        sm.start_practice = AsyncMock(side_effect=DraftPendingError())
        # Must not raise — a controller chord can't surface a 500.
        await sm.route_event(ControllerCommandEvent(command="toggle_practice"))
        sm.start_practice.assert_awaited_once()
```

- [ ] **Step 2: Run them — verify they fail**

Run: `python -m pytest tests/unit/test_session_manager.py::TestTogglePracticeCommand -q`
Expected: FAIL — `toggle_practice` is an unknown command (handler logs a warning + returns), so start/stop are never called.

- [ ] **Step 3: Restructure the handler + add the toggle helper**

In `python/spinlab/session_manager.py`, add `ActionError` to the `from .errors import (...)` block (it's the base class of the errors already imported there).

Replace the current `_handle_controller_command` (it has a single `pause` branch) with:

```python
    async def _handle_controller_command(self, event: ControllerCommandEvent) -> None:
        # Per-command dispatch; each command carries its own mode rule. The
        # input layer (detector) is mode-agnostic — it just names the command.
        if event.command == "pause":
            # Practice-scoped: pause only applies to a practice attempt.
            if self.mode == Mode.PRACTICE and self.practice_session:
                await self.practice_session.toggle_pause()
        elif event.command == "toggle_practice":
            await self._toggle_practice_from_menu()
        else:
            logger.warning("unknown controller command: %r", event.command)
            return
        await self._notify_sse()

    async def _toggle_practice_from_menu(self) -> None:
        """R+Y: start practice from IDLE, stop it from PRACTICE; ignore in any
        other mode. Swallows ActionError (pending draft / not connected /
        snapshot failure) — a controller chord must never crash the event loop
        or surface a 500; it just no-ops + logs."""
        try:
            if self.mode == Mode.IDLE:
                await self.start_practice()
            elif self.mode == Mode.PRACTICE:
                await self.stop_practice()
        except ActionError as exc:
            logger.info("toggle_practice ignored: %s", exc.detail)
```

> Note: `start_practice` / `stop_practice` each call `_notify_sse` internally; the trailing `_notify_sse` in the handler is a harmless second push (and the only push for the `pause` branch, which doesn't broadcast itself).

- [ ] **Step 4: Run them — verify they pass**

Run: `python -m pytest tests/unit/test_session_manager.py::TestTogglePracticeCommand -q`
Expected: PASS.

- [ ] **Step 5: Confirm no regression in the handler's other paths**

Run: `python -m pytest tests/unit/test_session_manager.py -q`
Expected: PASS (the existing `TestRMenuRouting` pause tests still pass).

- [ ] **Step 6: Statics**

Run: `ruff check python/spinlab/session_manager.py` and `npx pyright python/spinlab/session_manager.py`
Expected: no NEW errors (report pre-existing vs introduced counts).

- [ ] **Step 7: Commit**

```bash
git add python/spinlab/session_manager.py tests/unit/test_session_manager.py
git commit -m "feat(session): R+Y toggle_practice dispatch (start/stop, mode-aware)"
```

---

### Task 3: Emulator confirmation — `$15` reads + dispatches on real RA

**Files:**
- Modify: `tests/integration/addresses.py`
- Create: `tests/integration/scenarios/menu_toggle.poke`
- Test: `tests/integration/test_transitions.py`

- [ ] **Step 1: Map `$15` for the poke parser / engine**

In `tests/integration/addresses.py`, add to `ADDR_MAP`:

```python
    "controller_held_1": _a.ADDR_CONTROLLER_HELD_1,
```

(This also makes the poke engine zero `$15` per scenario, so a stale `$15` can't fire a phantom `toggle_practice` in other scenarios.)

- [ ] **Step 2: Create the scenario**

Create `tests/integration/scenarios/menu_toggle.poke`:

```
# menu_toggle — hold R ($17 0x10) to open the menu, then tap Y ($15 0x40) to
# dispatch 'toggle_practice'. R is a pure modifier (opens instantly); Y is not
# held at the open frame, so it's a clean rising edge.
settle: 30

1: controller_held=0x10
5: controller_held_1=0x40
```

- [ ] **Step 3: Write the failing emulator test**

In `tests/integration/test_transitions.py`, append (it already has `pytestmark = [pytest.mark.emulator, ...]` and the `run_scenario` fixture):

```python
async def test_r_menu_toggle_practice_command(run_scenario):
    """Hold R, tap Y on real RA -> one ControllerCommandEvent('toggle_practice').
    This is the live confirmation that $15 (the B/Y/Select/d-pad held byte) reads
    and dispatches on real RA."""
    from spinlab.protocol import ControllerCommandEvent
    events = await run_scenario("menu_toggle.poke")
    cmds = [e for e in events if isinstance(e, ControllerCommandEvent)]
    assert len(cmds) == 1, f"expected 1 command, got {cmds}"
    assert cmds[0].command == "toggle_practice"
```

- [ ] **Step 4: Run it — verify it passes (requires RA)**

Run: `python -m pytest tests/integration/test_transitions.py::test_r_menu_toggle_practice_command -m emulator -q`
Expected: PASS. A `SKIPPED` here is a FAILURE (the harness self-launches RA) — surface why before continuing. If it fails with the menu never dispatching, that means `$15` is mis-read on real RA — report the full event list.

Also run the full transitions file to confirm the `$15`-zeroing didn't disturb existing scenarios:

Run: `python -m pytest tests/integration/test_transitions.py -m emulator -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add tests/integration/addresses.py tests/integration/scenarios/menu_toggle.poke tests/integration/test_transitions.py
git commit -m "test(emulator): confirm 15 byte reads + R+Y toggle dispatch on real RA"
```

---

### Task 4: Verification gate

- [ ] **Step 1: Fast suite + statics**

```bash
python -m pytest -m "not emulator" -q
npx pyright python/spinlab/session_manager.py python/spinlab/retroarch/menu_detector.py
ruff check python/spinlab/
```
Expected: all green; no new pyright/ruff errors in touched files.

- [ ] **Step 2: Full suite (incl. emulator) — the merge gate**

```bash
python -m pytest
```
Expected: all pass, **zero skips**. The new `menu_toggle` emulator test must actually run.

- [ ] **Step 3: Optional manual smoke (RA + dashboard)**

From Idle, hold R + tap Y → practice starts. From Practice, hold R + tap Y → practice stops. (Pause R+X still works.)

---

## Self-review notes (checked against the spec, Phase 2 section)

- **Per-command dispatch with mode rules** → Task 2 (`_handle_controller_command` restructure).
- **`pause` → practice-only (shipped), `toggle_practice` → Idle starts / Practice stops** → Task 2 (`_toggle_practice_from_menu`), with `ActionError` swallowed (decision 2).
- **Ship Toggle Practice (R+Y)** → Tasks 1 (detector registers Y) + 2 (dispatch).
- **First real-RA `$15` confirmation** → Task 3 (the `menu_toggle` emulator scenario) — this is the live `$15` check Phase 1 deferred.
- Mode rule lives with the handler, not the detector (the detector just names the command). Consistent with the spec.
