# Direct-Gamepad Menu Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the WRAM `$15`/`$17` R-menu with a menu driven by reading the physical gamepad directly, so every physical button is distinct (fixing the 8bitdo face-button merge) and the modifier can sit on a no-game-function button.

**Architecture:** A new `spinlab.gamepad` package holds a source-agnostic `ControllerMenuDetector` (generalized from `(byte,bit)` keys to opaque integer button IDs), a `ButtonSource` protocol with a pygame-backed implementation, and a daemon-thread poll loop that steps the detector at ~60 Hz and forwards the existing `ControllerCommandEvent` / `ControllerMenuArmedEvent` into the orchestrator's asyncio event queue via `loop.call_soon_threadsafe`. The detector's output events and the entire `SessionManager` command dispatch are reused untouched. The WRAM menu reading is deleted from the poller and the controller bytes are dropped from `MemorySnapshot`.

**Tech Stack:** Python 3.11+, `pygame` (new optional dependency, `joystick` module), asyncio, daemon `threading.Thread`, pytest.

## Global Constraints

- **No magic numbers.** Every numeric constant gets a named file-level variable with a comment explaining *why* (CLAUDE.md). The poll period and any timing constant must be named.
- **No silent fallbacks on misconfiguration.** A `gamepad.enabled: true` config with an unknown verb or a missing modifier is a config error → raise `ValueError` (fail loud, matching `AppConfig.from_yaml`'s "crashes loud on missing required keys"). The ONLY graceful degradation is environmental: missing `pygame` or no controller connected → menu inactive (empty button set), never a crash.
- **pygame is an optional extra** (`pip install -e ".[gamepad]"`). Code must guard the import so the app runs without it.
- **Verbs are fixed:** `pause`, `toggle_science`, `toggle_practice`, `prev_segment`, `next_segment`. Defined once as `MENU_VERBS` and matched exactly — these strings are what `SessionManager._handle_controller_command` dispatches on (do NOT touch that handler).
- **Windows host.** Daemon threads are fine; long-running visual processes are not started by tests. The `gamepad-probe` CLI is interactive and run manually by Andrew.
- **TDD throughout:** failing test → run-it-fails → minimal impl → run-it-passes → commit. Fast suite is `pytest -m "not emulator"`.

---

### Task 1: `pygame` optional dependency + `gamepad` config section

**Files:**
- Modify: `pyproject.toml:11-30` (add a `gamepad` extra)
- Modify: `python/spinlab/config.py` (add `GamepadConfig`, an `AppConfig.gamepad` field, parsing)
- Modify: `config.example.yaml` (add a commented, disabled `gamepad:` section)
- Test: `tests/unit/test_config.py` (gamepad parsing cases)

**Interfaces:**
- Produces: `GamepadConfig(enabled: bool, device_index: int, modifier: int | None, buttons: dict[str, int])` — a frozen-ish dataclass; `buttons` maps **verb → button id** (the inverse of what the detector wants). `AppConfig.gamepad: GamepadConfig`.

- [ ] **Step 1: Write the failing config tests**

Add to `tests/unit/test_config.py` inside `class TestAppConfig`:

```python
    def test_gamepad_defaults_to_disabled(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({"data": {"dir": "data"}}))
        cfg = AppConfig.from_yaml(config_file)
        assert cfg.gamepad.enabled is False
        assert cfg.gamepad.device_index == 0
        assert cfg.gamepad.modifier is None
        assert cfg.gamepad.buttons == {}

    def test_gamepad_parses_full_section(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({
            "data": {"dir": "data"},
            "gamepad": {
                "enabled": True,
                "device_index": 1,
                "modifier": 8,
                "buttons": {
                    "pause": 9,
                    "toggle_science": 10,
                    "toggle_practice": 11,
                    "prev_segment": 4,
                    "next_segment": 5,
                },
            },
        }))
        cfg = AppConfig.from_yaml(config_file)
        assert cfg.gamepad.enabled is True
        assert cfg.gamepad.device_index == 1
        assert cfg.gamepad.modifier == 8
        assert cfg.gamepad.buttons["toggle_practice"] == 11
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/unit/test_config.py -k gamepad -v`
Expected: FAIL with `AttributeError: 'AppConfig' object has no attribute 'gamepad'`.

- [ ] **Step 3: Add `GamepadConfig` and wire it into `AppConfig`**

In `python/spinlab/config.py`, add the import for `field` and the dataclass (place after `EmulatorConfig`):

```python
from dataclasses import dataclass, field
```

```python
@dataclass
class GamepadConfig:
    """Physical-gamepad menu mapping. Read directly off the pad (pygame), not
    the SMW-emulated input. ``buttons`` maps a menu verb to a pygame button
    index; ``modifier`` is the button held to open the menu. IDs are
    controller/mode specific — discover them with ``spinlab gamepad-probe``.
    Disabled by default; with no pad connected the menu is simply inactive.
    """
    enabled: bool = False
    device_index: int = 0          # which joystick (pygame index)
    modifier: int | None = None    # button id that OPENS the menu (held)
    buttons: dict[str, int] = field(default_factory=dict)  # verb -> button id
```

Add the field to `AppConfig` as an **optional, defaulted** field at the END of
the field list (after `category`). It must have a default because `AppConfig` is
constructed directly in many test fixtures (`tests/conftest.py`'s
`make_test_config`, `dashboard.py`, etc.) — a required field would break every
such call site. A defaulted "disabled" gamepad is also the semantically correct
default and matches the project's "defaults in dataclass defaults" guideline.
Dataclass ordering requires defaulted fields last, so it goes after `category`:

```python
    network: NetworkConfig
    emulator: EmulatorConfig
    practice_engine: PracticeEngineConfig
    data_dir: Path
    rom_dir: Path | None
    category: str = "any%"
    # Optional physical-gamepad menu; defaults to disabled so existing configs
    # and direct AppConfig(...) constructions need not specify it.
    gamepad: GamepadConfig = field(default_factory=GamepadConfig)
```

In `AppConfig.from_yaml`, after the `pe = raw.get("practice_engine", {})` line add:

```python
        gp = raw.get("gamepad", {})
```

and in the `return cls(` block, after the `practice_engine=...` argument, add:

```python
            gamepad=GamepadConfig(
                enabled=bool(gp.get("enabled", False)),
                device_index=int(gp.get("device_index", 0)),
                modifier=gp.get("modifier"),
                buttons=dict(gp.get("buttons", {})),
            ),
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/unit/test_config.py -v`
Expected: PASS (all existing config tests + the two new ones).

- [ ] **Step 5: Add the `gamepad` extra and the example config**

In `pyproject.toml`, under `[project.optional-dependencies]`, add a new extra (after the `dev = [...]` line):

```toml
# Physical-gamepad menu input. pygame's joystick module reads the pad directly
# (8bitdo in X-input or D-input mode). Optional: without it the gamepad menu is
# inactive and the rest of the app runs unaffected.
gamepad = ["pygame>=2.5,<3"]
```

In `config.example.yaml`, append a commented section (so a real config opts in by uncommenting and filling real indices from the probe):

```yaml

# Physical-gamepad menu (optional; requires `pip install -e ".[gamepad]"`).
# Reads the controller directly, not the emulated SNES input. Button ids are
# pygame indices and vary per controller/mode and your RetroArch remaps — run
# `spinlab gamepad-probe` to discover them. Pick a no-game-function button for
# `modifier` (e.g. a stick click) since it is HELD to open the menu.
# gamepad:
#   enabled: true
#   device_index: 0
#   modifier: 8
#   buttons:
#     pause: 9
#     toggle_science: 10
#     toggle_practice: 11
#     prev_segment: 4
#     next_segment: 5
```

- [ ] **Step 6: Run the fast suite, then commit**

Run: `pytest -m "not emulator" -q`
Expected: PASS (no regressions).

```bash
git add pyproject.toml python/spinlab/config.py config.example.yaml tests/unit/test_config.py
git commit -m "feat(gamepad): add GamepadConfig + pygame optional extra"
```

---

### Task 2: Generalize `ControllerMenuDetector` to opaque button IDs (move to `gamepad` package)

**Files:**
- Create: `python/spinlab/gamepad/__init__.py`
- Create: `python/spinlab/gamepad/menu_detector.py`
- Create: `tests/unit/gamepad/__init__.py` (empty, only if the package needs it — match the existing `tests/unit/retroarch/` layout; create only if that dir has one)
- Create: `tests/unit/gamepad/test_menu_detector.py`

**Note:** The old `python/spinlab/retroarch/menu_detector.py` and its test are NOT deleted here — the poller still imports the old detector until Task 5 retires it. The new module lives at a different path (`spinlab.gamepad.menu_detector`) with no name collision, so both coexist and the full fast suite stays green after this task. Task 5 deletes the old module + test.

**Interfaces:**
- Consumes: `ControllerCommandEvent`, `ControllerMenuArmedEvent` from `spinlab.protocol` (unchanged).
- Produces:
  - `ButtonId = int`
  - `MENU_VERBS: frozenset[str]` = `{"pause", "toggle_science", "toggle_practice", "prev_segment", "next_segment"}`
  - `ControllerMenuDetector(*, modifier: ButtonId, commands: dict[ButtonId, str])` with `step(self, pressed: set[ButtonId]) -> list[ControllerCommandEvent | ControllerMenuArmedEvent]` and `reset(self) -> None`.

Note: `step` now takes a **set of held button IDs** (not a `MemorySnapshot`). The state machine is otherwise identical: modifier-held → `MenuArmed(True)`; seed currently-held command buttons on arm; fire each command on its rising edge; release → `MenuArmed(False)`.

- [ ] **Step 1: Write the failing detector tests**

Create `tests/unit/gamepad/test_menu_detector.py`:

```python
"""ControllerMenuDetector — button-ID state machine (gamepad-driven)."""
from spinlab.gamepad.menu_detector import ControllerMenuDetector
from spinlab.protocol import ControllerCommandEvent, ControllerMenuArmedEvent

# Arbitrary button ids for the tests — opaque integers, not SNES bits.
MOD = 8
PAUSE = 9
PRACTICE = 11

_MAPPING = {PAUSE: "pause", PRACTICE: "toggle_practice"}


def _det() -> ControllerMenuDetector:
    return ControllerMenuDetector(modifier=MOD, commands=_MAPPING)


def _run(det, frames):
    out = []
    for held in frames:
        out.extend(det.step(set(held)))
    return out


def _cmds(events):
    return [e.command for e in events if isinstance(e, ControllerCommandEvent)]


def _armed(events):
    return [e.armed for e in events if isinstance(e, ControllerMenuArmedEvent)]


def test_modifier_opens_menu_immediately():
    assert _armed(_run(_det(), [{MOD}])) == [True]


def test_command_pressed_after_modifier_fires():
    events = _run(_det(), [{MOD}, {MOD, PAUSE}])
    assert _cmds(events) == ["pause"]


def test_command_already_held_at_open_is_seeded_no_fire():
    # PAUSE held when MOD goes down must NOT fire (seeded as already-seen).
    events = _run(_det(), [{PAUSE}, {MOD, PAUSE}])
    assert _cmds(events) == []


def test_simultaneous_chord_is_seeded_then_fresh_press_fires():
    events = _run(_det(), [{MOD, PAUSE}, {MOD}, {MOD, PAUSE}])
    assert _cmds(events) == ["pause"]


def test_command_does_not_refire_while_held():
    events = _run(_det(), [{MOD}] + [{MOD, PAUSE}] * 5)
    assert _cmds(events) == ["pause"]


def test_release_modifier_closes_menu():
    assert _armed(_run(_det(), [{MOD}, set()])) == [True, False]


def test_lone_command_without_modifier_does_nothing():
    assert _run(_det(), [{PAUSE}, {PAUSE}]) == []


def test_two_commands_fire_in_sorted_order_same_frame():
    events = _run(_det(), [{MOD}, {MOD, PAUSE, PRACTICE}])
    # 9 (pause) sorts before 11 (toggle_practice).
    assert _cmds(events) == ["pause", "toggle_practice"]


def test_reset_clears_state():
    det = _det()
    det.step({MOD})
    det.reset()
    # After reset, MOD held again must re-emit armed=True (was open before reset).
    assert _armed(det.step({MOD})) == [True]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/unit/gamepad/test_menu_detector.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'spinlab.gamepad'`.

- [ ] **Step 3: Create the package and the refactored detector**

Create `python/spinlab/gamepad/__init__.py`:

```python
"""Physical-gamepad menu input. Reads the controller directly (pygame), not the
SMW-emulated SNES input, so every physical button is distinct and the modifier
can sit on a no-game-function button. Replaces the retired WRAM R-menu."""
```

Create `python/spinlab/gamepad/menu_detector.py`:

```python
"""ControllerMenuDetector — the menu command layer, source-agnostic.

A state machine over the set of physical buttons held this poll. One button is
a held MODIFIER: while it is down the command menu is open, and pressing a
command button dispatches the mapped verb. Releasing the modifier closes it.

The modifier is a pure modifier with no hold-time threshold — the menu opens the
instant it is held. The only subtlety is "press the command *after* the
modifier": a command button already held when the modifier goes down does NOT
fire (it is seeded as already-seen). This keeps the gesture precise and prevents
an accidental command when a gameplay button is held and the modifier is tapped.

Buttons are opaque integer IDs (pygame button indices). The detector knows
nothing about practice/pause — it only turns held-button sets into
ControllerMenuArmedEvent / ControllerCommandEvent, which SessionManager routes.
The modifier and command mapping are injected (from config); there are no
hardcoded button defaults because real indices are controller-specific.
"""
from __future__ import annotations

from spinlab.protocol import ControllerCommandEvent, ControllerMenuArmedEvent

# A physical button is identified by an opaque integer (a pygame button index).
ButtonId = int

# The fixed menu vocabulary. These strings are exactly what
# SessionManager._handle_controller_command dispatches on; config mappings are
# validated against this set (see build_gamepad_loop).
MENU_VERBS: frozenset[str] = frozenset(
    {"pause", "toggle_science", "toggle_practice", "prev_segment", "next_segment"}
)

_MenuEvent = ControllerCommandEvent | ControllerMenuArmedEvent


class ControllerMenuDetector:
    """Per-poll menu emitter. Stateful but pure (no IO)."""

    def __init__(
        self,
        *,
        modifier: ButtonId,
        commands: dict[ButtonId, str],
    ) -> None:
        self._modifier = modifier
        self._commands = commands
        self._menu_open = False
        # Command buttons held last poll (only meaningful while open) — used to
        # fire on the rising edge instead of every poll.
        self._prev_pressed: set[ButtonId] = set()

    def reset(self) -> None:
        self._menu_open = False
        self._prev_pressed = set()

    def step(self, pressed: set[ButtonId]) -> list[_MenuEvent]:
        events: list[_MenuEvent] = []
        mod_down = self._modifier in pressed
        pressed_cmds = {b for b in self._commands if b in pressed}

        if mod_down and not self._menu_open:
            # Modifier just went down — open the menu. Seed with the commands
            # ALREADY held so they don't count as a press; only a fresh press
            # fires.
            self._menu_open = True
            self._prev_pressed = pressed_cmds
            events.append(ControllerMenuArmedEvent(armed=True))
        elif not mod_down and self._menu_open:
            self._menu_open = False
            self._prev_pressed = set()
            events.append(ControllerMenuArmedEvent(armed=False))

        # DISPATCH: while open, each command fires on its rising edge. Sorted for
        # deterministic event order if several rise on the same poll.
        if self._menu_open:
            for button in sorted(pressed_cmds - self._prev_pressed):
                events.append(ControllerCommandEvent(command=self._commands[button]))
            self._prev_pressed = pressed_cmds

        return events
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/unit/gamepad/test_menu_detector.py -v`
Expected: PASS (all 9 tests).

- [ ] **Step 5: Run the full fast suite to confirm no regression**

The old detector is untouched, so nothing existing breaks; the new package is purely additive.

Run: `pytest -m "not emulator" -q`
Expected: PASS (all existing tests + the new gamepad detector tests).

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/gamepad/__init__.py python/spinlab/gamepad/menu_detector.py tests/unit/gamepad/
git commit -m "feat(gamepad): button-ID ControllerMenuDetector in new gamepad package"
```

---

### Task 3: `ButtonSource` protocol + pygame-backed `GamepadButtonSource`

**Files:**
- Create: `python/spinlab/gamepad/source.py`
- Create: `tests/unit/gamepad/test_source.py`

**Interfaces:**
- Consumes: `ButtonId` from `spinlab.gamepad.menu_detector`.
- Produces:
  - `ButtonSource` (Protocol) with `pressed(self) -> set[ButtonId]`.
  - `GamepadButtonSource(device_index: int = 0)` implementing it, with a read-only `available: bool` property.
  - Module function `_import_pygame()` (the test seam — returns the `pygame` module or raises).

The pygame wrapper is thin and covered automatically only for its **degradation guard** (missing pygame / no controller → empty set, never a crash). Real button reading is covered by the manual smoke test in Step 6.

- [ ] **Step 1: Write the failing source tests**

Create `tests/unit/gamepad/test_source.py`:

```python
"""GamepadButtonSource — the pygame wrapper's degradation guard.

Real button reads need a physical pad and are smoke-tested manually. Here we
only prove that a missing pygame / failed init degrades to an empty set and an
`available is False` flag, never a crash.
"""
import pytest

from spinlab.gamepad import source as source_mod
from spinlab.gamepad.source import GamepadButtonSource


def test_missing_pygame_degrades_to_inactive(monkeypatch):
    def _boom():
        raise ImportError("pygame not installed")
    monkeypatch.setattr(source_mod, "_import_pygame", _boom)

    src = GamepadButtonSource(device_index=0)
    assert src.available is False
    assert src.pressed() == set()


def test_init_failure_degrades_to_inactive(monkeypatch):
    class _FakePygame:
        class error(Exception):
            pass
        class joystick:
            @staticmethod
            def init():
                raise RuntimeError("no joystick subsystem")
        @staticmethod
        def init():
            pass
    monkeypatch.setattr(source_mod, "_import_pygame", lambda: _FakePygame)

    src = GamepadButtonSource(device_index=0)
    assert src.available is False
    assert src.pressed() == set()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/unit/gamepad/test_source.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'spinlab.gamepad.source'`.

- [ ] **Step 3: Implement the guarded pygame source**

Create `python/spinlab/gamepad/source.py`:

```python
"""ButtonSource — the menu detector's input. A real pad (pygame) in production,
a fake in tests. The pygame wrapper guards every entry point: missing pygame or
no controller degrades to an empty held-set so the menu is simply inactive,
never a crash."""
from __future__ import annotations

import logging
import os
from typing import Protocol

from spinlab.gamepad.menu_detector import ButtonId

logger = logging.getLogger(__name__)


class ButtonSource(Protocol):
    """Returns the set of physical buttons held this poll."""

    def pressed(self) -> set[ButtonId]:
        ...


def _import_pygame():
    """Import pygame, configuring SDL for a windowless joystick-only run.

    Isolated into a module function so tests can monkeypatch it to simulate a
    missing/failing pygame without touching the real import machinery. The dummy
    video driver lets the joystick subsystem init on a headless box; on a normal
    desktop it is harmless (we never open a window).
    """
    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    import pygame
    return pygame


class GamepadButtonSource:
    """pygame-backed ButtonSource. Opens one joystick by index and reports its
    held buttons each poll. Any import/init/read failure flips ``available`` to
    False and returns an empty set."""

    def __init__(self, device_index: int = 0) -> None:
        self._device_index = device_index
        self._pygame = None
        self._joystick = None
        self._available = False
        self._init()

    @property
    def available(self) -> bool:
        return self._available

    def _init(self) -> None:
        try:
            pygame = _import_pygame()
            pygame.init()
            pygame.joystick.init()
            if pygame.joystick.get_count() <= self._device_index:
                logger.warning(
                    "gamepad: no joystick at index %d (count=%d); menu inactive",
                    self._device_index, pygame.joystick.get_count(),
                )
                return
            joystick = pygame.joystick.Joystick(self._device_index)
            joystick.init()
            self._pygame = pygame
            self._joystick = joystick
            self._available = True
            logger.info(
                "gamepad: opened joystick %d (%s, %d buttons)",
                self._device_index, joystick.get_name(), joystick.get_numbuttons(),
            )
        except Exception as exc:  # ImportError, pygame.error, anything
            logger.warning("gamepad: init failed (%s); menu inactive", exc)
            self._available = False

    def pressed(self) -> set[ButtonId]:
        if not self._available or self._joystick is None or self._pygame is None:
            return set()
        try:
            # SDL needs its event queue pumped before get_button reflects the
            # current hardware state.
            self._pygame.event.pump()
            n = self._joystick.get_numbuttons()
            return {i for i in range(n) if self._joystick.get_button(i)}
        except Exception as exc:
            logger.warning("gamepad: read failed (%s); menu inactive", exc)
            self._available = False
            return set()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/unit/gamepad/test_source.py -v`
Expected: PASS (both guard tests).

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/gamepad/source.py tests/unit/gamepad/test_source.py
git commit -m "feat(gamepad): pygame ButtonSource with import/read guards"
```

- [ ] **Step 6: Manual smoke test (record result in the commit/PR, do NOT add to CI)**

This is the only coverage of real pygame button reads — no gamepad in CI. With the 8bitdo connected and `pip install -e ".[gamepad]"` done, run a throwaway REPL snippet and confirm distinct indices light up for each physical button (the whole point — the face buttons must no longer merge):

```python
from spinlab.gamepad.source import GamepadButtonSource
import time
s = GamepadButtonSource(0)
print("available:", s.available)
for _ in range(200):
    p = s.pressed()
    if p:
        print(sorted(p))
    time.sleep(0.05)
```

Expected: `available: True`; pressing X vs Y (and A vs B) prints *different* index sets. If they still merge, the controller is in a mode that collapses them — note it; the probe (Task 7) is the tool to map around it. No code change gated on this; it is a confirmation.

---

### Task 4: `GamepadMenuLoop` daemon thread + `build_gamepad_loop` factory

**Files:**
- Create: `python/spinlab/gamepad/loop.py`
- Create: `tests/unit/gamepad/test_loop.py`

**Interfaces:**
- Consumes: `ButtonSource`, `GamepadButtonSource` (source.py); `ControllerMenuDetector`, `MENU_VERBS`, `ButtonId` (menu_detector.py); `GamepadConfig` (config.py).
- Produces:
  - `GamepadMenuLoop(*, source: ButtonSource, detector: ControllerMenuDetector, period_sec: float = GAMEPAD_POLL_PERIOD_SEC)` with:
    - `bind(self, *, loop, on_event) -> None` — capture the running asyncio loop and the enqueue callback (called once at orchestrator connect time).
    - `start(self) -> None` — spawn the daemon thread.
    - `stop(self) -> None` — signal stop and join (bounded).
    - `_poll_once(self) -> None` — one iteration (testable: reads source, steps detector, forwards each event via `loop.call_soon_threadsafe(on_event, ev)`).
  - `build_gamepad_loop(gp: GamepadConfig) -> GamepadMenuLoop | None` — returns `None` when `gp.enabled` is False; raises `ValueError` on a misconfigured-but-enabled section (missing modifier, unknown verb); otherwise constructs source + detector + loop.

- [ ] **Step 1: Write the failing loop + factory tests**

Create `tests/unit/gamepad/test_loop.py`:

```python
"""GamepadMenuLoop poll/forward logic + build_gamepad_loop config binding."""
import pytest

from spinlab.config import GamepadConfig
from spinlab.gamepad.loop import GamepadMenuLoop, build_gamepad_loop
from spinlab.gamepad.menu_detector import ControllerMenuDetector
from spinlab.protocol import ControllerCommandEvent, ControllerMenuArmedEvent

MOD = 8
PAUSE = 9


class _FakeSource:
    def __init__(self, frames):
        self._frames = list(frames)
    def pressed(self):
        return set(self._frames.pop(0)) if self._frames else set()


class _RecordingLoop:
    """Stand-in for an asyncio loop: runs the callback inline and records it."""
    def __init__(self):
        self.events = []
    def call_soon_threadsafe(self, fn, arg):
        fn(arg)
        self.events.append(arg)


def _loop_with(frames):
    det = ControllerMenuDetector(modifier=MOD, commands={PAUSE: "pause"})
    return GamepadMenuLoop(source=_FakeSource(frames), detector=det)


def test_poll_once_forwards_arm_then_command():
    rec = _RecordingLoop()
    received = []
    gl = _loop_with([{MOD}, {MOD, PAUSE}])
    gl.bind(loop=rec, on_event=received.append)

    gl._poll_once()  # MOD down -> armed
    gl._poll_once()  # PAUSE after MOD -> command

    armed = [e.armed for e in received if isinstance(e, ControllerMenuArmedEvent)]
    cmds = [e.command for e in received if isinstance(e, ControllerCommandEvent)]
    assert armed == [True]
    assert cmds == ["pause"]


def test_build_returns_none_when_disabled():
    assert build_gamepad_loop(GamepadConfig(enabled=False)) is None


def test_build_raises_on_enabled_without_modifier():
    gp = GamepadConfig(enabled=True, modifier=None, buttons={"pause": 9})
    with pytest.raises(ValueError, match="modifier"):
        build_gamepad_loop(gp)


def test_build_raises_on_unknown_verb():
    gp = GamepadConfig(enabled=True, modifier=8, buttons={"explode": 9})
    with pytest.raises(ValueError, match="explode"):
        build_gamepad_loop(gp)


def test_build_constructs_loop_with_inverted_mapping():
    gp = GamepadConfig(
        enabled=True, modifier=8,
        buttons={"pause": 9, "toggle_practice": 11},
    )
    gl = build_gamepad_loop(gp)
    assert isinstance(gl, GamepadMenuLoop)
    # The detector must map button-id -> verb (inverse of config's verb -> id).
    assert gl._detector._commands == {9: "pause", 11: "toggle_practice"}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/unit/gamepad/test_loop.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'spinlab.gamepad.loop'`.

- [ ] **Step 3: Implement the loop and factory**

Create `python/spinlab/gamepad/loop.py`:

```python
"""GamepadMenuLoop — a daemon thread that polls the gamepad and forwards menu
events into the asyncio event loop.

SDL's event pump prefers a single owning thread, so the poll runs on a dedicated
daemon thread (off the async loop). Each emitted event is marshalled back onto
the loop with ``call_soon_threadsafe`` so it reaches SessionManager.route_event
on the loop thread, exactly like poller-emitted events.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from spinlab.config import GamepadConfig
from spinlab.gamepad.menu_detector import MENU_VERBS, ControllerMenuDetector
from spinlab.gamepad.source import ButtonSource, GamepadButtonSource

logger = logging.getLogger(__name__)

# Poll the pad once per frame at 60 Hz — fast enough that a tap between two
# polls is never missed at human press durations (the prior WRAM poller ran at
# the same rate), cheap enough to leave a core idle. Matches the detector's
# rising-edge gesture, which assumes per-frame granularity.
GAMEPAD_POLL_PERIOD_SEC = 1.0 / 60.0

# How long stop() waits for the daemon thread to drain before giving up. The
# thread checks the stop flag every poll period, so two periods is ample; we
# never block shutdown on a wedged SDL read.
_STOP_JOIN_TIMEOUT_SEC = 0.5


class GamepadMenuLoop:
    def __init__(
        self,
        *,
        source: ButtonSource,
        detector: ControllerMenuDetector,
        period_sec: float = GAMEPAD_POLL_PERIOD_SEC,
    ) -> None:
        self._source = source
        self._detector = detector
        self._period = period_sec
        self._loop = None
        self._on_event: Callable[[object], None] | None = None
        self._thread: threading.Thread | None = None
        self._stopped = False

    def bind(self, *, loop, on_event: Callable[[object], None]) -> None:
        """Capture the running asyncio loop and the enqueue callback. Called
        once, from the loop thread, before start()."""
        self._loop = loop
        self._on_event = on_event

    def start(self) -> None:
        if self._loop is None or self._on_event is None:
            raise RuntimeError("GamepadMenuLoop.start() before bind()")
        self._stopped = False
        self._thread = threading.Thread(
            target=self._run, name="gamepad-menu", daemon=True,
        )
        self._thread.start()
        logger.info("gamepad menu loop started")

    def stop(self) -> None:
        self._stopped = True
        if self._thread is not None:
            self._thread.join(timeout=_STOP_JOIN_TIMEOUT_SEC)
            self._thread = None

    def _poll_once(self) -> None:
        pressed = self._source.pressed()
        for event in self._detector.step(pressed):
            # Marshal onto the asyncio loop thread; bind() guarantees both set.
            self._loop.call_soon_threadsafe(self._on_event, event)

    def _run(self) -> None:
        while not self._stopped:
            try:
                self._poll_once()
            except Exception as exc:
                # Never let the daemon die on a transient read hiccup.
                logger.warning("gamepad poll iteration failed: %s", exc)
            time.sleep(self._period)


def build_gamepad_loop(gp: GamepadConfig) -> GamepadMenuLoop | None:
    """Construct a GamepadMenuLoop from config, or None when disabled.

    Fails loud on a misconfigured-but-enabled section: a missing modifier or an
    unknown verb is an operator error, not something to silently swallow. The
    only graceful degradation (missing pygame / no pad) happens inside
    GamepadButtonSource, so a returned loop may still be inactive at runtime.
    """
    if not gp.enabled:
        return None
    if gp.modifier is None:
        raise ValueError("gamepad.enabled is true but gamepad.modifier is unset")
    unknown = set(gp.buttons) - MENU_VERBS
    if unknown:
        raise ValueError(
            f"gamepad.buttons has unknown verb(s): {sorted(unknown)}; "
            f"valid verbs are {sorted(MENU_VERBS)}"
        )
    # Config maps verb -> button id; the detector wants button id -> verb.
    commands = {button_id: verb for verb, button_id in gp.buttons.items()}
    source = GamepadButtonSource(device_index=gp.device_index)
    detector = ControllerMenuDetector(modifier=gp.modifier, commands=commands)
    return GamepadMenuLoop(source=source, detector=detector)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/unit/gamepad/test_loop.py -v`
Expected: PASS (all 5 tests).

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/gamepad/loop.py tests/unit/gamepad/test_loop.py
git commit -m "feat(gamepad): daemon poll loop + build_gamepad_loop factory"
```

---

### Task 5: Remove the WRAM menu from the poller and snapshot

**Files:**
- Modify: `python/spinlab/retroarch/poller.py` (drop the menu detector entirely)
- Modify: `python/spinlab/retroarch/snapshot.py` (drop controller fields + their cluster read; preserve the write-barrier note on the new first read)
- Modify: `tests/unit/retroarch/test_poller.py` (remove the menu-forwarding test)
- Modify: `tests/unit/retroarch/test_snapshot.py` (remove controller assertions + cluster)
- Delete: `python/spinlab/retroarch/menu_detector.py` (the old WRAM detector, superseded by `spinlab.gamepad.menu_detector` from Task 2)
- Delete: `tests/unit/retroarch/test_menu_detector.py` (the old WRAM-bit detector tests)
- Delete: `tests/integration/scenarios/menu_pause.poke`, `menu_toggle.poke`, `menu_nav.poke`, `menu_science.poke`
- Modify: `tests/integration/test_transitions.py` (remove the four `test_r_menu_*` tests)

**Interfaces:**
- Consumes: nothing new.
- Produces: `Poller` constructor no longer accepts a `menu=` argument; `MemorySnapshot` no longer has `controller_held` / `controller_held_1`; `read_snapshot` no longer reads `$15..$17`.

- [ ] **Step 1: Update the poller test first (red)**

In `tests/unit/retroarch/test_poller.py`, delete the entire `test_poller_forwards_menu_events` function (lines ~337-366, including the `_StubMenu` class defined inside it).

Run: `pytest tests/unit/retroarch/test_poller.py -v`
Expected: still PASS for the rest — confirms nothing else references the menu. (If another test references `menu=`, fix it here.)

- [ ] **Step 2: Remove the menu from `poller.py`**

First delete the old detector module and its test (superseded by Task 2's `spinlab.gamepad.menu_detector`):

```bash
git rm python/spinlab/retroarch/menu_detector.py tests/unit/retroarch/test_menu_detector.py
```

Then in `python/spinlab/retroarch/poller.py`:

1. Delete the import `from spinlab.retroarch.menu_detector import ControllerMenuDetector` (line ~26).
2. Delete the `menu: ControllerMenuDetector | None = None` parameter from `Poller.__init__` (line ~67) and the `self._menu = menu if menu is not None else ControllerMenuDetector()` assignment (line ~74).
3. Delete `self._menu_failing: bool = False` (line ~92).
4. Delete the entire menu-step block in `run()` (lines ~246-265): the `try/except` around `self._menu.step(snap)` and the `for mev in menu_events:` forwarding loop.

- [ ] **Step 3: Run the poller test to verify green**

Run: `pytest tests/unit/retroarch/test_poller.py -v`
Expected: PASS.

- [ ] **Step 4: Update the snapshot test (red)**

In `tests/unit/retroarch/test_snapshot.py`:
- Remove the two controller entries from `addr_to_value` (the `0x0017` and `0x0015` lines).
- Remove the first cluster `(0x0015, 3)` from the `clusters` list.
- Remove the two assertions `assert snap.controller_held == 0xCC` and `assert snap.controller_held_1 == 0xDD`.

Run: `pytest tests/unit/retroarch/test_snapshot.py -v`
Expected: FAIL — `read_snapshot` still issues the `$15` cluster read which the fake server no longer has a canned reply for (or the snapshot still has the fields). This confirms the test now pins the new shape.

- [ ] **Step 5: Remove the controller bytes from `snapshot.py`**

In `python/spinlab/retroarch/snapshot.py`:
1. Delete the `controller_held: int = 0` and `controller_held_1: int = 0` fields (and their comments) from `MemorySnapshot`.
2. In `read_snapshot`, delete the `c_ctrl = client.read_ram(...)` call and the `controller_held_1 = ...` / `controller_held = ...` lines.
3. Delete `controller_held=controller_held` and `controller_held_1=controller_held_1` from the `MemorySnapshot(...)` return.
4. Preserve the write-barrier behaviour: the FIRST `read_ram` is now the `c_low` ($0071) read. Replace the `$15..$17` docstring/comment block with a note that `c_low` is now the barrier read. Add this comment immediately above the `c_low = ...` line:

```python
    # $0071..$010B read FIRST as the write-barrier: in the poke harness,
    # FRAMEADVANCE is fire-and-forget, so the first NCI read of the snapshot
    # forces RA to drain its write queue (FRAMEADVANCE + any WRITE_CORE_RAM)
    # before replying. Any read works as the barrier; this is just the first.
    c_low = client.read_ram(a.ADDR_PLAYER_ANIM, a.ADDR_ROOM_NUM - a.ADDR_PLAYER_ANIM + 1)
```

Also update the function docstring: it says "Read all 13 SMW state bytes ... 7 ... reads" — change to "11 SMW state bytes ... 6 ... reads" (two controller bytes and their cluster are gone). Verify the count by reading the final `read_ram` calls.

- [ ] **Step 6: Run the snapshot test to verify green**

Run: `pytest tests/unit/retroarch/test_snapshot.py -v`
Expected: PASS.

- [ ] **Step 7: Delete the obsolete WRAM menu integration scenarios + tests**

```bash
git rm tests/integration/scenarios/menu_pause.poke tests/integration/scenarios/menu_toggle.poke tests/integration/scenarios/menu_nav.poke tests/integration/scenarios/menu_science.poke
```

In `tests/integration/test_transitions.py`, delete the four functions `test_r_menu_pause_command`, `test_r_menu_toggle_practice_command`, `test_r_menu_next_segment_command`, `test_r_menu_toggle_science_command` (lines ~122-169). The physical gamepad menu has no live-RA path (no pad in CI); the detector is covered by the new unit tests.

- [ ] **Step 8: Run the fast suite and commit**

Run: `pytest -m "not emulator" -q`
Expected: PASS — confirms the deleted `menu_detector` module has no lingering importers and the snapshot change is clean.

```bash
git add python/spinlab/retroarch/poller.py python/spinlab/retroarch/snapshot.py tests/unit/retroarch/test_poller.py tests/unit/retroarch/test_snapshot.py tests/integration/test_transitions.py
git commit -m "refactor(retroarch): retire WRAM menu reading from poller + snapshot"
```

---

### Task 6: Wire the gamepad loop into the orchestrator and `build_orchestrator`

**Files:**
- Modify: `python/spinlab/retroarch/orchestrator.py` (accept `gamepad`, start in `connect()`, stop in `disconnect()`)
- Modify: `python/spinlab/retroarch/wiring.py` (build the loop from config, pass it in)
- Modify: `tests/unit/retroarch/test_orchestrator.py` (a fake gamepad loop; assert bind/start on connect, stop on disconnect)

**Interfaces:**
- Consumes: `build_gamepad_loop` (loop.py), `GamepadMenuLoop`.
- Produces: `RetroArchOrchestrator.__init__` gains a keyword-only `gamepad: GamepadMenuLoop | None = None`. On `connect()` (when non-None) it calls `gamepad.bind(loop=asyncio.get_running_loop(), on_event=self._enqueue)` then `gamepad.start()`; on `disconnect()` it calls `gamepad.stop()`.

- [ ] **Step 1: Write the failing orchestrator wiring test**

Add to `tests/unit/retroarch/test_orchestrator.py` (a self-contained test; it builds an orchestrator directly so it can inject the fake gamepad):

```python
@pytest.mark.asyncio
async def test_gamepad_loop_started_on_connect_stopped_on_disconnect(tmp_path):
    from spinlab.retroarch.movies import MovieController

    class _FakeGamepad:
        def __init__(self):
            self.bound = None
            self.started = False
            self.stopped = False
        def bind(self, *, loop, on_event):
            self.bound = (loop, on_event)
        def start(self):
            self.started = True
        def stop(self):
            self.stopped = True

    gamepad = _FakeGamepad()
    raclient = FakeRAClient()
    poller = FakePoller()
    movies = MovieController(
        movie_io=FakeMovieIO(), raclient=raclient, enable=False,
        on_event=lambda ev: None,
    )
    orch = RetroArchOrchestrator(
        raclient=raclient,
        poller=poller,
        conditions=ConditionRegistry(),
        practice_timing=PracticeTiming(),
        hyper_play_timing=HyperPlayTiming(),
        state_paths=StatePathResolver(tmp_path),
        movies=movies,
        gamepad=gamepad,
    )
    await orch.connect()
    assert gamepad.started is True
    assert gamepad.bound is not None
    # on_event is the orchestrator's enqueue, so an event reaches orch.events.
    _loop, on_event = gamepad.bound
    on_event("sentinel")
    assert orch.events.get_nowait() == "sentinel"

    await orch.disconnect()
    assert gamepad.stopped is True
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/unit/retroarch/test_orchestrator.py -k gamepad -v`
Expected: FAIL with `TypeError: __init__() got an unexpected keyword argument 'gamepad'`.

- [ ] **Step 3: Add the `gamepad` hook to the orchestrator**

In `python/spinlab/retroarch/orchestrator.py`:

1. Add the constructor parameter (keyword-only, after `movies`):

```python
        movies: MovieController,
        gamepad=None,
    ) -> None:
```

2. Store it (after `self._movies = movies`):

```python
        self._gamepad = gamepad
```

3. In `connect()`, after the poller/tick tasks are created (after line ~168 `self._tick_task = asyncio.create_task(self._tick_loop())`), add:

```python
        if self._gamepad is not None:
            self._gamepad.bind(
                loop=asyncio.get_running_loop(),
                on_event=self._enqueue,
            )
            self._gamepad.start()
```

4. In `disconnect()`, after the poller is stopped (after the `self._poller_task = None` block, before the tick-task block), add:

```python
        if self._gamepad is not None:
            self._gamepad.stop()
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/unit/retroarch/test_orchestrator.py -k gamepad -v`
Expected: PASS.

- [ ] **Step 5: Wire `build_gamepad_loop` into `build_orchestrator`**

In `python/spinlab/retroarch/wiring.py`, inside `build_orchestrator`, after the `poller = Poller(...)` line (~104) and before `movies = MovieController(...)`, add:

```python
    from spinlab.gamepad.loop import build_gamepad_loop
    gamepad = build_gamepad_loop(config.gamepad)
```

Then pass it into the orchestrator constructor (add to the `RetroArchOrchestrator(...)` call after `movies=movies,`):

```python
        movies=movies,
        gamepad=gamepad,
    )
```

- [ ] **Step 6: Run the fast suite and commit**

Run: `pytest -m "not emulator" -q`
Expected: PASS.

```bash
git add python/spinlab/retroarch/orchestrator.py python/spinlab/retroarch/wiring.py tests/unit/retroarch/test_orchestrator.py
git commit -m "feat(gamepad): start/stop gamepad menu loop with the orchestrator"
```

---

### Task 7: `spinlab gamepad-probe` CLI

**Files:**
- Create: `python/spinlab/gamepad/probe.py`
- Modify: `python/spinlab/cli.py` (register the `gamepad-probe` subcommand + dispatch)
- Test: `tests/unit/gamepad/test_probe.py` (device-list formatting — the pollable, non-interactive part)

**Interfaces:**
- Consumes: `_import_pygame` (source.py) for device enumeration.
- Produces: `python/spinlab/gamepad/probe.py` with `list_devices() -> list[str]` (names, index-ordered) and `run_probe(device_index: int = 0) -> int` (the interactive loop; returns an exit code). `cli.py` gains a `gamepad-probe` subcommand with `--device` (int, default 0).

- [ ] **Step 1: Write the failing probe test**

Create `tests/unit/gamepad/test_probe.py`:

```python
"""gamepad-probe device enumeration (the non-interactive, testable part)."""
from spinlab.gamepad import probe as probe_mod
from spinlab.gamepad.probe import list_devices


def test_list_devices_returns_names_in_index_order(monkeypatch):
    class _Joy:
        def __init__(self, i):
            self._i = i
        def init(self):
            pass
        def get_name(self):
            return f"Pad{self._i}"
    class _FakePygame:
        class joystick:
            @staticmethod
            def init():
                pass
            @staticmethod
            def get_count():
                return 2
            Joystick = _Joy
        @staticmethod
        def init():
            pass
    monkeypatch.setattr(probe_mod, "_import_pygame", lambda: _FakePygame)

    assert list_devices() == ["Pad0", "Pad1"]


def test_list_devices_empty_when_pygame_missing(monkeypatch):
    def _boom():
        raise ImportError("no pygame")
    monkeypatch.setattr(probe_mod, "_import_pygame", _boom)
    assert list_devices() == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/unit/gamepad/test_probe.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'spinlab.gamepad.probe'`.

- [ ] **Step 3: Implement the probe**

Create `python/spinlab/gamepad/probe.py`:

```python
"""`spinlab gamepad-probe` — print button indices as they are pressed so the
user can fill in config.yaml's `gamepad.buttons`. The pygame button index of a
physical button varies per controller and mode (X-input vs D-input) and is not
guessable; this is the tool that maps them. Interactive; run from a terminal."""
from __future__ import annotations

import time

# Re-export the import seam so tests can monkeypatch enumeration without a pad.
from spinlab.gamepad.source import _import_pygame

# How often the probe samples the pad while waiting for presses. Matches the
# menu loop's 60 Hz so what you see is what the menu will read.
_PROBE_POLL_PERIOD_SEC = 1.0 / 60.0


def list_devices() -> list[str]:
    """Names of connected joysticks, index-ordered. Empty if pygame is missing
    or no pad is present."""
    try:
        pygame = _import_pygame()
        pygame.init()
        pygame.joystick.init()
        names = []
        for i in range(pygame.joystick.get_count()):
            joy = pygame.joystick.Joystick(i)
            joy.init()
            names.append(joy.get_name())
        return names
    except Exception:
        return []


def run_probe(device_index: int = 0) -> int:
    """Interactive: print each button index on its rising edge until Ctrl-C."""
    devices = list_devices()
    if not devices:
        print("No gamepad detected (or pygame not installed: pip install -e '.[gamepad]').")
        return 1
    print("Detected devices:")
    for i, name in enumerate(devices):
        marker = " <- probing" if i == device_index else ""
        print(f"  [{i}] {name}{marker}")
    if device_index >= len(devices):
        print(f"\nNo device at index {device_index}.")
        return 1

    pygame = _import_pygame()
    joy = pygame.joystick.Joystick(device_index)
    joy.init()
    print(
        f"\nProbing '{joy.get_name()}' ({joy.get_numbuttons()} buttons). "
        "Press buttons to see their indices; Ctrl-C to quit.\n"
    )
    prev: set[int] = set()
    try:
        while True:
            pygame.event.pump()
            now = {i for i in range(joy.get_numbuttons()) if joy.get_button(i)}
            for i in sorted(now - prev):
                print(f"  button {i} pressed")
            prev = now
            time.sleep(_PROBE_POLL_PERIOD_SEC)
    except KeyboardInterrupt:
        print("\nDone.")
        return 0
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/unit/gamepad/test_probe.py -v`
Expected: PASS.

- [ ] **Step 5: Register the CLI subcommand**

In `python/spinlab/cli.py`, in `main()`, after the `p_db` block (and before the `cli_fit_pool` registration), add:

```python
    p_probe = sub.add_parser(
        "gamepad-probe", help="Print gamepad button indices as you press them"
    )
    p_probe.add_argument(
        "--device", type=int, default=0, help="Joystick index to probe (default 0)"
    )
```

And in the dispatch chain (after the `elif parsed.command == "db":` block), add:

```python
    elif parsed.command == "gamepad-probe":
        from spinlab.gamepad.probe import run_probe
        sys.exit(run_probe(parsed.device))
```

- [ ] **Step 6: Verify the CLI wires up (non-interactive smoke)**

Run: `python -m spinlab.cli gamepad-probe --help`
Expected: prints the subcommand help with `--device`, exit 0.

(Full interactive behaviour — pressing buttons prints indices — is verified manually by Andrew with the pad; it cannot run in CI.)

- [ ] **Step 7: Run the fast suite and commit**

Run: `pytest -m "not emulator" -q`
Expected: PASS.

```bash
git add python/spinlab/gamepad/probe.py python/spinlab/cli.py tests/unit/gamepad/test_probe.py
git commit -m "feat(gamepad): spinlab gamepad-probe CLI for discovering button ids"
```

---

### Task 8: Generalize the route-bar menu hint

**Files:**
- Modify: `frontend/src/route-bar.ts:77-79` (hint text no longer names SNES buttons)
- Test: `frontend/src/route-bar.test.ts` (if it asserts the old hint text — update; otherwise add a minimal assertion)

**Interfaces:**
- Consumes: `rs.menu_armed` (unchanged — the gamepad detector still emits `ControllerMenuArmedEvent`, so `menu_armed` still drives this hint).
- Produces: a generalized hint that does not promise specific physical buttons (they are user-configured now).

- [ ] **Step 1: Check the existing route-bar test for the hint**

Run: `grep -n "menu_armed\|menuHint\|R-menu\|rb-menu-hint" frontend/src/route-bar.test.ts`
If a test asserts the literal old string (`R-menu · Y start/stop · X pause ...`), it must be updated in Step 3. If none exists, add the assertion in Step 2.

- [ ] **Step 2: Write/adjust the failing frontend test**

In `frontend/src/route-bar.test.ts`, add (or update) a test asserting the generalized hint appears when armed and is absent otherwise. Use the file's existing render helper / fixture shape (match how other tests there build the `data`/`rs` object):

```ts
it("shows a generalized menu hint when the gamepad menu is armed", () => {
  const html = renderRouteBar(makeData({ menu_armed: true }));
  expect(html).toContain("Menu armed");
  expect(html).not.toContain("R-menu");
});
```

(Adapt `renderRouteBar` / `makeData` to the actual helpers in the test file.)

- [ ] **Step 3: Run the frontend test to verify it fails**

Run: `cd frontend && npm test -- route-bar`
Expected: FAIL (the current hint contains `R-menu`, not `Menu armed`).

- [ ] **Step 4: Generalize the hint text**

In `frontend/src/route-bar.ts`, replace the `menuHint` assignment:

```ts
  const menuHint = rs.menu_armed
    ? `<div class="rb-menu-hint">Menu armed · pause · science · start/stop · ◄ ► segment</div>`
    : "";
```

(The verbs stay; the physical-button names are dropped because they are configured per controller.)

- [ ] **Step 5: Run the frontend test to verify it passes, then build**

Run: `cd frontend && npm test -- route-bar`
Expected: PASS.

Run: `cd frontend && npm run build`
Expected: succeeds (needed before the Python frontend smoke tests run).

- [ ] **Step 6: Commit**

```bash
git add frontend/src/route-bar.ts frontend/src/route-bar.test.ts
git commit -m "feat(gamepad): generalize route-bar menu hint (buttons now configured)"
```

---

### Task 9: Full-suite verification + memory update

**Files:**
- None (verification + docs/memory)

- [ ] **Step 1: Build the frontend (required for the Python frontend smoke tests)**

Run: `cd frontend && npm run build`
Expected: succeeds.

- [ ] **Step 2: Run the FULL suite (unit + emulator + frontend), per CLAUDE.md**

Run: `python -m pytest`
Expected: ALL PASS, **zero skips** beyond pre-accepted `skipif`s. Emulator tests must actually run (RAHarness self-launches RA). If emulator tests fail or skip with a launch error, surface it — do not declare done. The deleted `menu_*.poke` scenarios should simply no longer be collected; confirm no integration test references them.

- [ ] **Step 3: Type-check and lint the new package**

Run: `npx pyright python/spinlab/gamepad/ && ruff check python/spinlab/gamepad/`
Expected: no NEW pyright errors in the new files; ruff clean. (`pygame` is an optional import — if pyright flags the `import pygame` as unresolved, that is expected for an optional extra; confirm it is only that and not a real type error.)

- [ ] **Step 4: Update the spec status and memory**

- Flip `docs/superpowers/specs/2026-06-19-direct-gamepad-menu-design.md` frontmatter `status: approved` → `status: implemented` (or append an "Implemented YYYY-MM-DD (commit)" line).
- Update memory `project_gamepad_menu_spec.md`: change "NOT yet planned/built" to record that the plan executed and the WRAM menu was retired, noting the new `spinlab.gamepad` package + `gamepad-probe` CLI and the `[gamepad]` extra.

- [ ] **Step 5: Final commit**

```bash
git add docs/superpowers/specs/2026-06-19-direct-gamepad-menu-design.md
git commit -m "docs(gamepad): mark direct-gamepad-menu spec implemented"
```

---

## Self-Review

**Spec coverage:**
- "Read the physical pad directly via pygame; retire the WRAM menu" → Tasks 3 (source), 5 (retire WRAM). ✓
- "Reuse ControllerMenuDetector's state machine, generalize (byte,bit) → button IDs" → Task 2. ✓
- "ButtonSource protocol so tests inject a fake; pygame-backed GamepadButtonSource" → Task 3. ✓
- "Daemon poll thread ~60Hz → call_soon_threadsafe → route_event" → Task 4 (loop) + Task 6 (wire to orch queue → route_event). ✓
- "Configurable, one global mapping in config.yaml (gamepad section)" → Task 1. ✓
- "Per-game/per-level overrides DEFERRED (config shaped to allow later)" → `GamepadConfig.buttons` is a flat verb→id map a future `per_game` wrapper can nest; not built. ✓
- "spinlab gamepad-probe CLI prints button indices" → Task 7. ✓
- "Verbs unchanged; SessionManager dispatch reused untouched" → no session_manager edit; `MENU_VERBS` matches the 5 handled verbs. ✓
- "Guard import so missing pygame / no controller degrades to inactive, never crash" → Task 3 guards; Task 4 factory only raises on *config* errors, not environment. ✓
- "Drop controller_held* from MemorySnapshot iff detector was their only consumer (verified)" → confirmed only menu_detector + snapshot consume them; Task 5 drops them + the cluster read, preserving the write-barrier on the new first read. ✓
- "Update/retire the route-bar R-menu hint" → Task 8. ✓
- "Detector full unit coverage via fake ButtonSource; pygame source thin + manual smoke" → Task 2 unit tests; Task 3 guard tests + manual smoke Step 6. ✓
- CRITICAL constraint (dirty-remapper, modifier must be no-game-function): honoured by making config mandatory, modifier any button id, and the example/probe guiding toward stick-clicks; encoded in config comments (Task 1) and `build_gamepad_loop` requiring a modifier. ✓

**Placeholder scan:** No "TBD"/"add error handling"/"write tests for the above" — every code step shows full code; every test step shows the test. ✓

**Task ordering / green-after-each-task:** Task 2 is purely additive (new `spinlab.gamepad` package; the old `retroarch/menu_detector.py` stays until Task 5), so the full fast suite is green after every task. Task 5 deletes the old detector + test as part of retiring the WRAM menu.

**Type consistency:** `ButtonId = int` defined in menu_detector, imported by source/loop. `ControllerMenuDetector(*, modifier, commands)` / `step(pressed: set)` consistent across Tasks 2, 4, 6 tests. `GamepadMenuLoop.bind(loop=, on_event=)` / `start()` / `stop()` consistent across Tasks 4 and 6. `build_gamepad_loop(gp) -> GamepadMenuLoop | None` consistent (Tasks 4, 6). `GamepadConfig(enabled, device_index, modifier, buttons)` consistent (Tasks 1, 4). `_import_pygame` seam shared by source + probe. ✓

**Note for executor:** All tasks are individually green — Task 2 is additive and the old detector is deleted only in Task 5. Run the full fast suite (`pytest -m "not emulator"`) as the per-task gate.
