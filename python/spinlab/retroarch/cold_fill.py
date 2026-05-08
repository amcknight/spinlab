"""ColdFillSpawnDetector — captures cold spawns after a reference run.

Per-segment observer (compare with TransitionDetector, which is per-frame
across the whole session). Activated externally with a segment id, watches
the death-then-spawn sequence, emits a single Spawn event with
is_cold_cp=True, deactivates.

Mirrors lua/spinlab.lua's handle_cold_fill — but extends it to also detect
death-falls (exit_mode going non-zero without a goal). Lua's narrow
`anim == 9` check missed pit-falls; in practice, many SMW deaths skip the
sprite-hit animation entirely and go straight from playing to falling
off-screen, which only shows up as an exit_mode change.
"""
from __future__ import annotations

from spinlab.retroarch import addresses as a
from spinlab.retroarch.events import Spawn
from spinlab.retroarch.predicates import (
    FANFARE_ACTIVE,
    LEVEL_START_ACTIVE,
    PLAYER_ANIM_DEAD,
)
from spinlab.retroarch.snapshot import MemorySnapshot


class ColdFillSpawnDetector:
    def __init__(self) -> None:
        self._active = False
        self._waiting_spawn = False  # False = waiting for death; True = waiting for spawn
        self._segment_id: str | None = None
        self._prev_anim = 0
        self._prev_level_start = 0
        self._prev_exit_mode = 0

    def is_active(self) -> bool:
        return self._active

    def activate(self, segment_id: str) -> None:
        self._active = True
        self._waiting_spawn = False
        self._segment_id = segment_id
        self._prev_anim = 0
        self._prev_level_start = 0
        self._prev_exit_mode = 0

    def step(self, curr: MemorySnapshot, timestamp_ms: int) -> Spawn | None:
        if not self._active:
            return None

        emitted: Spawn | None = None

        if not self._waiting_spawn:
            died_sprite = (
                curr.player_anim == PLAYER_ANIM_DEAD
                and self._prev_anim != PLAYER_ANIM_DEAD
            )
            # Pit-fall / death-fall: exit_mode goes non-zero without any goal
            # signal. The detector emits LevelExit for the same edge but with
            # goal_type='abort'; we treat that as a death too. Guard against
            # goal-triggered exits by checking fanfare and io_port.
            died_via_exit = (
                curr.exit_mode != 0 and self._prev_exit_mode == 0
                and curr.fanfare != FANFARE_ACTIVE
                and curr.io_port not in (a.IO_GOAL, a.IO_ORB, a.IO_KEY)
            )
            if died_sprite or died_via_exit:
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
            # CP-respawn path: in some hacks the level isn't reloaded after a
            # death — the player just respawns at the CP. level_start may stay
            # at 1 throughout, OR briefly drop to 0 during the transition;
            # similarly exit_mode 1->0 may not coincide with level_start=1 on
            # the same frame. Edge-triggered checks (edge_spawn, fast_retry)
            # miss both. Once we're already in waiting_spawn (we KNOW a death
            # fired), the simplest robust signal is level-triggered: fire on
            # the first frame the player is back in playable state.
            playable = (
                curr.exit_mode == 0
                and curr.level_start == LEVEL_START_ACTIVE
                and curr.player_anim != PLAYER_ANIM_DEAD
            )
            if edge_spawn or fast_retry or playable:
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
        self._prev_exit_mode = curr.exit_mode
        return emitted
