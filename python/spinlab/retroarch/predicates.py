"""Pure detection predicates — port of lua/spinlab.lua transition functions.

Every function here takes a previous snapshot, a current snapshot, and
optionally a TransitionState. None mutate any state. Their return values are
the source of truth for what the polling loop turns into events.
"""
from __future__ import annotations

from spinlab.retroarch import addresses as a
from spinlab.retroarch.snapshot import MemorySnapshot
from spinlab.retroarch.transition_state import TransitionState

PLAYER_ANIM_DEAD = 9
FANFARE_ACTIVE = 1  # SMW fanfare byte: steps to this value when goal is reached and stays.
LEVEL_START_ACTIVE = 1  # SMW level_start byte: set to this when the player appears in a level.


def is_death_frame(prev: MemorySnapshot, curr: MemorySnapshot) -> bool:
    """Edge-triggered so the caller gets one death event per transition, not one per frame while the animation sustains."""
    return curr.player_anim == PLAYER_ANIM_DEAD and prev.player_anim != PLAYER_ANIM_DEAD


def is_exit_frame(prev: MemorySnapshot, curr: MemorySnapshot) -> bool:
    """SMW's exit_mode byte stays non-zero through the whole exit sequence; the edge check gives one event per exit."""
    return curr.exit_mode != 0 and prev.exit_mode == 0


def goal_type(curr: MemorySnapshot) -> str:
    """Classify the current goal state of a level exit.

    Mirrors lua/spinlab.lua `goal_type`: precedence is key > orb > boss > normal,
    with anything else treated as 'abort' (e.g. start+select reset, death exit).
    """
    if curr.io_port == a.IO_KEY:
        return "key"
    if curr.io_port == a.IO_ORB:
        return "orb"
    if curr.boss_defeat != 0 and curr.fanfare == FANFARE_ACTIVE:
        return "boss"
    if curr.fanfare == FANFARE_ACTIVE or curr.io_port == a.IO_GOAL:
        return "normal"
    return "abort"


def check_checkpoint_hit(
    prev: MemorySnapshot, curr: MemorySnapshot, state: TransitionState
) -> str | None:
    """Returns "midway" or "cp_entrance" if a checkpoint fired this frame, else None.

    Suppressed if any goal-type signal also fired this frame (orb/goal/key/fadeout)
    — those events take precedence and the checkpoint detection would be a
    spurious side effect.
    """
    got_orb = curr.io_port == a.IO_ORB
    got_goal = curr.fanfare == FANFARE_ACTIVE or curr.io_port == a.IO_GOAL
    got_key = curr.io_port == a.IO_KEY
    got_fadeout = curr.io_port == a.IO_FADEOUT
    blocked = got_orb or got_goal or got_key or got_fadeout

    midway_hit = (prev.midway == 0 and curr.midway == 1) and not blocked
    cp_entrance_hit = (
        curr.level_num != 0
        and curr.cp_entrance != prev.cp_entrance
        and curr.cp_entrance != state.first_cp_entrance
        and not blocked
    )

    if midway_hit:
        return "midway"
    if cp_entrance_hit:
        return "cp_entrance"
    return None


def detect_finish(prev: MemorySnapshot, curr: MemorySnapshot) -> str | None:
    """Early finish detection (kaizosplits LevelFinish).

    Returns "normal" / "boss" / "orb" / "key" if one fired this frame, else None.
    Edge-triggered on the relevant transitions.
    """
    # Goal tape: fanfare 0 -> 1, boss alive, no orb.
    if curr.fanfare == FANFARE_ACTIVE and prev.fanfare == 0 and curr.boss_defeat == 0 and curr.io_port != a.IO_ORB:
        return "normal"
    # Boss: fanfare 0 -> 1, boss defeated.
    if curr.fanfare == FANFARE_ACTIVE and prev.fanfare == 0 and curr.boss_defeat != 0:
        return "boss"
    # Orb: io shifts to 3, boss alive.
    if curr.io_port == a.IO_ORB and prev.io_port != a.IO_ORB and curr.boss_defeat == 0:
        return "orb"
    # Key: io shifts to 7.
    if curr.io_port == a.IO_KEY and prev.io_port != a.IO_KEY:
        return "key"
    return None
