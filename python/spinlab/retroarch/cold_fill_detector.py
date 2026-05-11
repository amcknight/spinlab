"""ColdFillSpawnDetector — captures cold spawns after a reference run.

Per-segment observer (compare with TransitionDetector, which is per-frame
across the whole session). Activated externally with a segment id, watches
the death-then-spawn sequence, emits a single Spawn event with
is_cold_cp=True, deactivates.

Death detection covers both the sprite-hit animation (player_anim == 9)
AND exit_mode going non-zero without a goal — many SMW deaths skip the
sprite-hit entirely and go straight from playing to falling off-screen.
"""
from __future__ import annotations

import logging

from spinlab.protocol import SpawnEvent
from spinlab.retroarch import addresses as a
from spinlab.retroarch.predicates import (
    FANFARE_ACTIVE,
    LEVEL_START_ACTIVE,
    PLAYER_ANIM_DEAD,
)
from spinlab.retroarch.snapshot import MemorySnapshot

logger = logging.getLogger(__name__)


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
        logger.info("cold_fill_detector: activated for segment=%s", segment_id)

    def resync_after_state_load(self, snapshot: MemorySnapshot) -> None:
        """Sync prev_* to the just-loaded snapshot to suppress phantom edges.

        Mirrors TransitionDetector.resync_after_state_load. Without this,
        activate() leaves prev_exit_mode=0; the first poll after a hot-state
        load reads curr.exit_mode=non-zero (whatever the saved state had),
        and died_via_exit fires immediately as a phantom death — the
        detector enters _waiting_spawn=True before the player has touched
        the controller. Observed live 2026-05-08 on L58 with a stale hot
        state where exit_mode loaded at 1.
        """
        self._waiting_spawn = False
        self._prev_anim = snapshot.player_anim
        self._prev_level_start = snapshot.level_start
        self._prev_exit_mode = snapshot.exit_mode

    def step(self, curr: MemorySnapshot, timestamp_ms: int) -> SpawnEvent | None:
        if not self._active:
            return None

        emitted: SpawnEvent | None = None

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
                logger.info(
                    "cold_fill_detector: death observed segment=%s "
                    "via_sprite=%s via_exit=%s — waiting for spawn",
                    self._segment_id, died_sprite, died_via_exit,
                )
        else:
            edge_spawn = (
                curr.level_start == LEVEL_START_ACTIVE and self._prev_level_start == 0
            )
            fast_retry = (
                curr.level_start == LEVEL_START_ACTIVE
                and curr.player_anim != PLAYER_ANIM_DEAD
                and self._prev_anim == PLAYER_ANIM_DEAD
            )
            # CP-respawn path: when the level isn't reloaded after a death,
            # neither $1935 (level_start) nor $0100 (game_mode) gives a
            # one-value "in playable level" check. game_mode is meaningful
            # but uses multiple values for different playable contexts —
            # observed live: 0x14 (Love Yourself L44) and 0x16 (Love Yourself
            # L58, sublevel 20). Enumerating every valid game_mode across
            # every hack is fragile. Since _waiting_spawn means we've ALREADY
            # observed a death, the only thing we need to detect is "death
            # sequence is over": exit_mode is back to 0 (not in any
            # death/exit/transition animation) AND player_anim is not the
            # death sprite. No level-identification needed.
            playable = (
                curr.exit_mode == 0
                and curr.player_anim != PLAYER_ANIM_DEAD
            )
            if edge_spawn or fast_retry or playable:
                via = (
                    "edge_spawn" if edge_spawn
                    else "fast_retry" if fast_retry
                    else "playable"
                )
                emitted = SpawnEvent(
                    timestamp_ms=timestamp_ms,
                    level_num=curr.level_num,
                    is_cold_cp=True,
                    cp_ordinal=0,
                    segment_id=self._segment_id or "",
                )
                logger.info(
                    "cold_fill_detector: spawn emitted via=%s segment=%s",
                    via, self._segment_id,
                )
                self._active = False
                self._waiting_spawn = False
                self._segment_id = None

        self._prev_anim = curr.player_anim
        self._prev_level_start = curr.level_start
        self._prev_exit_mode = curr.exit_mode
        return emitted
