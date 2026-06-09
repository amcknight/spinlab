"""ControllerMenuDetector — the R-menu command layer.

A poller-level state machine over the per-frame held-button byte. R (controller-1
$17, bit 0x10) is a held MODIFIER: while R is down the command menu is open, and
pressing a command button dispatches the mapped command. Releasing R closes it.

R is a pure modifier with no hold-time threshold — the menu opens the instant R
is held, so the gesture feels immediate. The only subtlety is "press X *after*
R": a command button that was already held when R went down does NOT fire (it's
seeded as already-seen). This keeps R+X precise and prevents an accidental
command when a gameplay button (e.g. X = run) is held and R is tapped — you
must press the command button fresh while R is held.

Single responsibility: it knows nothing about practice/pause — it only turns
controller input into ControllerMenuArmedEvent / ControllerCommandEvent, which
SessionManager routes. The command registry is built to extend (add a button
bit -> command name); X = pause is the only command today.
"""
from __future__ import annotations

from spinlab.protocol import ControllerCommandEvent, ControllerMenuArmedEvent
from spinlab.retroarch.snapshot import MemorySnapshot

# $17 bit layout is A X L R - - - - (kaizosplits buttonsHeld2).
BUTTON_R = 0x10  # bit 4 — opens the menu (held modifier)
BUTTON_X = 0x40  # bit 6 — first command button

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
        self._menu_open = False  # True while R is held
        # Command-button bits seen held last frame (only meaningful while the
        # menu is open) — used to fire on the rising edge instead of every frame.
        self._prev_command_bits = 0

    def reset(self) -> None:
        self._menu_open = False
        self._prev_command_bits = 0

    def step(self, snap: MemorySnapshot) -> list[_MenuEvent]:
        events: list[_MenuEvent] = []
        held = snap.controller_held
        r_down = bool(held & BUTTON_R)

        if r_down and not self._menu_open:
            # R just went down — open the menu. Seed prev-bits with whatever
            # command buttons are ALREADY held so they don't count as a press;
            # only a fresh press while R is held dispatches ("X after R").
            self._menu_open = True
            self._prev_command_bits = held & COMMAND_MASK
            events.append(ControllerMenuArmedEvent(armed=True))
        elif not r_down and self._menu_open:
            self._menu_open = False
            self._prev_command_bits = 0
            events.append(ControllerMenuArmedEvent(armed=False))

        # DISPATCH: while open, each command button fires on its rising edge.
        if self._menu_open:
            for bit, command in COMMANDS.items():
                if (held & bit) and not (self._prev_command_bits & bit):
                    events.append(ControllerCommandEvent(command=command))
            self._prev_command_bits = held & COMMAND_MASK

        return events
