"""ControllerMenuDetector — the R-menu command layer.

A poller-level state machine over the per-frame held-button bytes. R
(controller-1 $17, bit 0x10) is a held MODIFIER: while R is down the command
menu is open, and pressing a command button dispatches the mapped command.
Releasing R closes it.

R is a pure modifier with no hold-time threshold — the menu opens the instant R
is held. The only subtlety is "press the command *after* R": a command button
already held when R goes down does NOT fire (it's seeded as already-seen). This
keeps the gesture precise and prevents an accidental command when a gameplay
button is held and R is tapped — you must press the command fresh while R is
held.

Buttons span two WRAM held bytes (kaizosplits naming): $17 (HELD2: A X L R) and
$15 (HELD1: B Y Select Start + d-pad). Each button — modifier or command — is
addressed by (snapshot-field, bit), so the registry can mix both bytes. The
modifier and the COMMANDS registry are constructor-injectable for testing; the
production poller uses the module defaults.

Single responsibility: it knows nothing about practice/pause — it only turns
controller input into ControllerMenuArmedEvent / ControllerCommandEvent, which
SessionManager routes.
"""
from __future__ import annotations

from spinlab.protocol import ControllerCommandEvent, ControllerMenuArmedEvent
from spinlab.retroarch.snapshot import MemorySnapshot

# Which MemorySnapshot field a button's byte lives in.
HELD2 = "controller_held"    # $17: A X L R - - - -
HELD1 = "controller_held_1"  # $15: B Y Select Start Up Down Left Right

# Button bits within their byte.
BUTTON_R = 0x10  # $17 — held modifier
BUTTON_X = 0x40  # $17 — pause
BUTTON_Y = 0x40  # $15 — toggle practice (same bit value as X, different byte)
BUTTON_LEFT = 0x02   # $15 — previous segment
BUTTON_RIGHT = 0x01  # $15 — next segment

# A button is a (snapshot-field, bit) pair.
ButtonKey = tuple[str, int]

# The modifier that opens the menu.
MODIFIER: ButtonKey = (HELD2, BUTTON_R)

# (snapshot-field, bit) -> command name. Spans both held bytes; extend by adding
# a key. X = pause, Y = toggle_practice, and left/right = prev/next segment are
# the commands today.
COMMANDS: dict[ButtonKey, str] = {
    (HELD2, BUTTON_X): "pause",
    (HELD1, BUTTON_Y): "toggle_practice",
    (HELD1, BUTTON_RIGHT): "next_segment",
    (HELD1, BUTTON_LEFT): "prev_segment",
}

_MenuEvent = ControllerCommandEvent | ControllerMenuArmedEvent


def _down(snap: MemorySnapshot, key: ButtonKey) -> bool:
    field, bit = key
    return bool(getattr(snap, field) & bit)


class ControllerMenuDetector:
    """Per-frame R-menu emitter. Stateful but pure (no IO)."""

    def __init__(
        self,
        *,
        modifier: ButtonKey = MODIFIER,
        commands: dict[ButtonKey, str] = COMMANDS,
    ) -> None:
        self._modifier = modifier
        self._commands = commands
        self._menu_open = False
        # Command keys held last frame (only meaningful while open) — used to
        # fire on the rising edge instead of every frame.
        self._prev_pressed: set[ButtonKey] = set()

    def reset(self) -> None:
        self._menu_open = False
        self._prev_pressed = set()

    def step(self, snap: MemorySnapshot) -> list[_MenuEvent]:
        events: list[_MenuEvent] = []
        r_down = _down(snap, self._modifier)
        pressed_now = {k for k in self._commands if _down(snap, k)}

        if r_down and not self._menu_open:
            # R just went down — open the menu. Seed with the commands ALREADY
            # held so they don't count as a press; only a fresh press fires.
            self._menu_open = True
            self._prev_pressed = pressed_now
            events.append(ControllerMenuArmedEvent(armed=True))
        elif not r_down and self._menu_open:
            self._menu_open = False
            self._prev_pressed = set()
            events.append(ControllerMenuArmedEvent(armed=False))

        # DISPATCH: while open, each command fires on its rising edge. Sorted for
        # deterministic event order if several rise on the same frame.
        if self._menu_open:
            for key in sorted(pressed_now - self._prev_pressed):
                events.append(ControllerCommandEvent(command=self._commands[key]))
            self._prev_pressed = pressed_now

        return events
