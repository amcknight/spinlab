"""ColdFillTracker — captures cold spawns after a reference run.

Activated externally with a segment id. Observes death-then-spawn sequence,
emits a single Spawn event with is_cold_cp=True, deactivates. Mirrors
lua/spinlab.lua's handle_cold_fill.
"""
from __future__ import annotations

from spinlab.retroarch.events import Spawn
from spinlab.retroarch.predicates import LEVEL_START_ACTIVE, PLAYER_ANIM_DEAD
from spinlab.retroarch.snapshot import MemorySnapshot


class ColdFillTracker:
    def __init__(self) -> None:
        self._active = False
        self._waiting_spawn = False  # False = waiting for death; True = waiting for spawn
        self._segment_id: str | None = None
        self._prev_anim = 0
        self._prev_level_start = 0

    def is_active(self) -> bool:
        return self._active

    def activate(self, segment_id: str) -> None:
        self._active = True
        self._waiting_spawn = False
        self._segment_id = segment_id
        self._prev_anim = 0
        self._prev_level_start = 0

    def step(self, curr: MemorySnapshot, timestamp_ms: int) -> Spawn | None:
        if not self._active:
            return None

        emitted: Spawn | None = None

        if not self._waiting_spawn:
            # Look for death.
            if curr.player_anim == PLAYER_ANIM_DEAD and self._prev_anim != PLAYER_ANIM_DEAD:
                self._waiting_spawn = True
        else:
            edge_spawn = (
                curr.level_start == LEVEL_START_ACTIVE and self._prev_level_start == 0
            )
            fast_retry = (
                curr.level_start == LEVEL_START_ACTIVE
                and curr.player_anim != PLAYER_ANIM_DEAD
                and self._prev_anim == PLAYER_ANIM_DEAD
            )
            if edge_spawn or fast_retry:
                emitted = Spawn(
                    timestamp_ms=timestamp_ms,
                    level_num=curr.level_num,
                    is_cold_cp=True,
                    cp_ordinal=0,
                    state_captured=True,
                    segment_id=self._segment_id or "",
                )
                self._active = False
                self._waiting_spawn = False
                self._segment_id = None

        self._prev_anim = curr.player_anim
        self._prev_level_start = curr.level_start
        return emitted
