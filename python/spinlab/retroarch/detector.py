"""TransitionDetector — stateful, pure-logic event emitter.

Drives one frame at a time via .step(snapshot, timestamp_ms). Maintains
prev-snapshot, transition state, cp_acquired, level_start_frame internally.
Returns a list of events emitted on this frame (often empty).

Caller (poller) is responsible for: fetching snapshots, supplying timestamps,
filling state_path on events that need them (since that depends on game_id
which the detector doesn't know about), and forwarding events downstream.
"""
from __future__ import annotations

from spinlab.protocol import (
    CheckpointEvent,
    DeathEvent,
    LevelEntranceEvent,
    LevelExitEvent,
    SpawnEvent,
)
from spinlab.retroarch.predicates import (
    LEVEL_START_ACTIVE,
    PLAYER_ANIM_DEAD,
    check_checkpoint_hit,
    goal_type,
    is_death_frame,
    is_exit_frame,
)
from spinlab.retroarch.snapshot import MemorySnapshot
from spinlab.retroarch.transition_state import TransitionState

FPS = 60.0  # SMW NTSC; close enough for elapsed-ms math

# Union of every concrete event type the detector emits. Listed for downstream
# type narrowing — protocol classes don't share a common base, so we enumerate.
_EmittedEvent = (
    LevelEntranceEvent | DeathEvent | CheckpointEvent | LevelExitEvent | SpawnEvent
)


class TransitionDetector:
    """Per-frame transition emitter. Stateful but pure (no IO)."""

    def __init__(self) -> None:
        self._prev: MemorySnapshot | None = None
        self._state = TransitionState()
        self._cp_acquired = False
        self._level_start_frame = 0
        self._frame_counter = 0
        self._exit_this_frame = False

    def reset(self) -> None:
        """Clear all state (for new segment / mode change / state-load)."""
        self._prev = None
        self._state.reset()
        self._cp_acquired = False
        self._level_start_frame = 0
        self._exit_this_frame = False

    def resync_after_state_load(self, snapshot: MemorySnapshot) -> None:
        """Replace prev wholesale after a save state load and clear flags.

        Mirrors lua/spinlab.lua's `state_just_loaded` re-sync: avoid phantom
        edge transitions on the first frame after load by treating the loaded
        state as if it were the previous frame's reading too.

        ALSO clears died_flag / cp_acquired / exit_this_frame: a state load
        is semantically a fresh start, and stale flags from before the load
        would suppress real events afterward (e.g., died_flag stuck from a
        previous death prevents subsequent Death events from firing — this
        bit practice mode hard, where every reload-on-death loaded the
        state but kept died_flag=True, blocking all later death detections).
        We do NOT reset _frame_counter (it's a monotonic session clock).
        """
        self._state.reset()
        self._cp_acquired = False
        self._exit_this_frame = False
        self._prev = snapshot

    def step(self, curr: MemorySnapshot, timestamp_ms: int) -> list[_EmittedEvent]:
        """Advance one frame; return list of transition events fired (often empty)."""
        self._frame_counter += 1
        events: list[_EmittedEvent] = []
        prev = self._prev
        if prev is None:
            self._prev = curr
            return events

        # 1. Death.
        if is_death_frame(prev, curr) and not self._state.died_flag:
            events.append(DeathEvent(timestamp_ms=timestamp_ms, level_num=curr.level_num))
            self._state.died_flag = True

        # 2. Checkpoint.
        cp_type = check_checkpoint_hit(prev, curr, self._state)
        if cp_type is not None:
            self._state.cp_ordinal += 1
            self._cp_acquired = True
            self._state.first_cp_entrance = 0  # opens cp_entrance shifts after first hit
            events.append(
                CheckpointEvent(
                    timestamp_ms=timestamp_ms,
                    level_num=curr.level_num,
                    cp_type=cp_type,
                    cp_ordinal=self._state.cp_ordinal,
                )
            )

        # 3. Exit (must come before entrance — see lua/spinlab.lua comment).
        self._exit_this_frame = is_exit_frame(prev, curr)
        if self._exit_this_frame:
            elapsed = int((self._frame_counter - self._level_start_frame) / FPS * 1000)
            events.append(
                LevelExitEvent(
                    timestamp_ms=timestamp_ms,
                    level=curr.level_num,
                    room=curr.room_num,
                    goal=goal_type(curr),
                    elapsed_ms=elapsed,
                    frame=self._frame_counter,
                )
            )

        # 4. Entrance: level_start 0->1 OR fast retry.
        edge_spawn = curr.level_start == LEVEL_START_ACTIVE and prev.level_start == 0
        fast_retry = (
            self._state.died_flag
            and curr.level_start == LEVEL_START_ACTIVE
            and curr.player_anim != PLAYER_ANIM_DEAD
            and prev.player_anim == PLAYER_ANIM_DEAD
        )
        if (edge_spawn or fast_retry) and not self._exit_this_frame:
            if self._state.died_flag:
                # Respawn after death.
                was_cp = self._cp_acquired
                if was_cp:
                    self._cp_acquired = False
                events.append(
                    SpawnEvent(
                        timestamp_ms=timestamp_ms,
                        level_num=curr.level_num,
                        is_cold_cp=was_cp,
                        cp_ordinal=self._state.cp_ordinal,
                        state_captured=was_cp,
                    )
                )
                self._state.died_flag = False
            else:
                # Fresh level entry.
                self._state.cp_ordinal = 0
                self._cp_acquired = False
                self._state.first_cp_entrance = curr.cp_entrance
                self._level_start_frame = self._frame_counter
                events.append(
                    LevelEntranceEvent(
                        timestamp_ms=timestamp_ms,
                        level=curr.level_num,
                        room=curr.room_num,
                        frame=self._frame_counter,
                    )
                )

        self._prev = curr
        return events
