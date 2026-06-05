"""TransitionState — per-segment mutable state for detection.

Cleared at the start of a new segment / mode change.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TransitionState:
    """Mutable per-frame state for transition detection.

    Attributes:
        died_flag: Set when the player dies during segment capture.
        cp_ordinal: Checkpoint ordinal number (0-indexed).
        first_room: Entry room (roomNum) captured on a levelNum shift. Used to
            exclude the cp_entrance shift that accompanies a fresh level entry
            (it shifts to the entry room) so it isn't read as a checkpoint.
            Mirrors kaizosplits' firstRoom. Cleared (to 0) when a CP fires.
        last_event_key: Key of the last detected event, or None.
    """

    died_flag: bool = False
    cp_ordinal: int = 0
    first_room: int = 0
    last_event_key: str | None = None

    def reset(self) -> None:
        """Reset all fields to their initial state.

        Called at the start of a new segment or mode change to clear
        all detection state.
        """
        self.died_flag = False
        self.cp_ordinal = 0
        self.first_room = 0
        self.last_event_key = None
