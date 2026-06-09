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
            # Seed prev-bits so a command button already held at the arm frame
            # is NOT a rising edge — the menu is a deliberate two-step gesture
            # (hold R to arm + show the hint, THEN press the command). Only a
            # fresh press after arming dispatches.
            self._prev_command_bits = held & COMMAND_MASK
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
