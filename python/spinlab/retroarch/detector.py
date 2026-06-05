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
    detect_finish,
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
        # True after detect_finish fires for this level; suppresses the later
        # is_exit_frame emission so only one LevelExitEvent fires per level.
        # Cleared on LevelEntranceEvent (new level) and state-load resync.
        self._finish_emitted = False
        # Replay-start entrance synthesis. `mark_replay_entrance` sets
        # `_replay_pending`; the state-load resync (PLAY_REPLAY bumps
        # state_version) converts it to `_force_next_entrance`, which `step`
        # consumes on the next frame regardless of level_start. See
        # `mark_replay_entrance` for the why.
        self._replay_pending = False
        self._force_next_entrance = False

    def reset(self) -> None:
        """Clear all state (for new segment / mode change / state-load)."""
        self._prev = None
        self._state.reset()
        self._cp_acquired = False
        self._level_start_frame = 0
        self._exit_this_frame = False
        self._finish_emitted = False
        self._replay_pending = False
        self._force_next_entrance = False

    def resync_after_state_load(self, snapshot: MemorySnapshot) -> bool:
        """Replace prev wholesale after a save state load and clear flags.

        Treats the loaded snapshot as the previous frame so the first frame
        after load doesn't fire phantom edge transitions.

        ALSO clears died_flag / cp_acquired / exit_this_frame: a state load
        is semantically a fresh start, and stale flags from before the load
        would suppress real events afterward (e.g., died_flag stuck from a
        previous death prevents subsequent Death events from firing — this
        bit practice mode hard, where every reload-on-death loaded the
        state but kept died_flag=True, blocking all later death detections).
        We do NOT reset _frame_counter (it's a monotonic session clock).

        A replay start is also a state load: PLAY_REPLAY bumps state_version, so
        this runs on the replay's first post-load frame. If a replay entrance is
        pending, arm it here — the *next* step fires it (this snapshot becomes
        prev), guaranteeing the entrance lands on a post-load frame with the
        replay's level_num. See `mark_replay_entrance`.

        Returns True if this resync armed a pending replay entrance (lets the
        poller log the replay-start signal for failure diagnostics).
        """
        self._state.reset()
        self._cp_acquired = False
        self._exit_this_frame = False
        self._finish_emitted = False
        self._prev = snapshot
        if self._replay_pending:
            self._force_next_entrance = True
            self._replay_pending = False
            return True
        return False

    def mark_replay_entrance(self) -> None:
        """Mark the next state-load resync as a replay entrance.

        Replay playback (PLAY_REPLAY) loads the replay's embedded savestate,
        which always begins at a level entrance. PLAY_REPLAY now bumps
        raclient.state_version (like load_state), so the poller resyncs on the
        replay's first post-load frame and `resync_after_state_load` converts
        this pending mark into an armed `_force_next_entrance`; the next `step`
        then synthesizes the `LevelEntranceEvent` regardless of level_start.

        Why not key off level_start directly: SMW holds level_start=1 for only
        a few frames at the entrance splash, then drops to 0 forever in a
        one-level replay. Under fast-forward, or when the shared NCI socket
        stalls the poller during play_movie's verify, the poller can step right
        over that brief window — and a missed first edge is a missed entrance
        forever (no segment recording starts, sections_captured stays 0, the
        replay-fixture test hangs to its 120s timeout). Arming at the resync
        and firing regardless of level_start removes that dependency. See
        docs/superpowers/plans/2026-05-18-mode2-replay-entrance-detection.md.

        Caller (MovieController.start_playback) invokes this BEFORE play_movie
        so the pending mark is set before PLAY_REPLAY's state_version bump
        triggers the resync.
        """
        self._replay_pending = True

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

        # Record the entry room when the level number changes, BEFORE the
        # checkpoint check, so check_checkpoint_hit can exclude the cp_entrance
        # shift that always accompanies a fresh level entry (cp_entrance shifts
        # to the entry room). Mirrors kaizosplits' firstRoom (Watchers.cs): set
        # on a levelNum shift, cleared when a CP fires. Without it the entrance
        # frame emits a phantom CheckpointEvent (→ a spurious second save
        # state). Backlog E: two save states on one level start.
        if curr.level_num != prev.level_num:
            self._state.first_room = curr.room_num

        # 2. Checkpoint.
        cp_type = check_checkpoint_hit(prev, curr, self._state)
        if cp_type is not None:
            self._state.cp_ordinal += 1
            self._cp_acquired = True
            self._state.first_room = 0  # kaizosplits clears firstRoom after a CP
            events.append(
                CheckpointEvent(
                    timestamp_ms=timestamp_ms,
                    level_num=curr.level_num,
                    cp_type=cp_type,
                    cp_ordinal=self._state.cp_ordinal,
                )
            )

        # 3. Exit must come before entrance: on a same-frame exit→entrance,
        #    the entrance check below would otherwise consume the level_start
        #    edge and we'd miss the exit event.
        #
        #    Prefer detect_finish (goal-tape / orb / key / boss edge) over the
        #    later is_exit_frame (exit_mode non-zero). detect_finish fires when
        #    the player actually reaches the goal — several frames before the
        #    exit animation begins — and is the only signal for game-ending
        #    boss defeats where exit_mode never fires (credits roll instead).
        _early_finish = detect_finish(prev, curr) if not self._finish_emitted else None
        _mode_exit = is_exit_frame(prev, curr)
        self._exit_this_frame = _early_finish is not None or _mode_exit
        elapsed = int((self._frame_counter - self._level_start_frame) / FPS * 1000)
        if _early_finish is not None:
            self._finish_emitted = True
            events.append(
                LevelExitEvent(
                    timestamp_ms=timestamp_ms,
                    level=curr.level_num,
                    room=curr.room_num,
                    goal=_early_finish,
                    elapsed_ms=elapsed,
                    frame=self._frame_counter,
                )
            )
        elif _mode_exit and not self._finish_emitted:
            # Abort (no fanfare/orb/key/boss) — exit_mode edge is the only signal.
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

        # 4. Entrance: level_start 0->1 OR fast retry OR replay-start force.
        edge_spawn = curr.level_start == LEVEL_START_ACTIVE and prev.level_start == 0
        fast_retry = (
            self._state.died_flag
            and curr.level_start == LEVEL_START_ACTIVE
            and curr.player_anim != PLAYER_ANIM_DEAD
            and prev.player_anim == PLAYER_ANIM_DEAD
        )
        # Replay start: the state-load resync armed this on the replay's first
        # post-load frame. Fire on the next step regardless of level_start —
        # the brief level_start=1 splash may have been missed entirely. See
        # `mark_replay_entrance`.
        forced_entrance = self._force_next_entrance
        if forced_entrance:
            self._force_next_entrance = False
        if (edge_spawn or fast_retry or forced_entrance) and not self._exit_this_frame:
            # Forced entrance always means "fresh level entry" regardless of
            # whatever died_flag the pre-replay snapshots left behind. A
            # replay starts from its embedded savestate; carrying over a
            # stale death flag would route us into the Respawn branch and
            # emit SpawnEvent instead of LevelEntranceEvent.
            if forced_entrance:
                self._state.died_flag = False
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
                    )
                )
                self._state.died_flag = False
            else:
                # Fresh level entry. (first_room is captured by the
                # levelNum-shift block above, kaizosplits-style.)
                self._state.cp_ordinal = 0
                self._cp_acquired = False
                self._finish_emitted = False
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
