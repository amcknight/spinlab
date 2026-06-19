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
