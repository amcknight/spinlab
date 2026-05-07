"""Pure predicate tests using synthetic snapshots."""
from spinlab.retroarch import addresses as a
from spinlab.retroarch.predicates import (
    check_checkpoint_hit,
    detect_finish,
    goal_type,
    is_death_frame,
    is_exit_frame,
)
from spinlab.retroarch.snapshot import MemorySnapshot
from spinlab.retroarch.transition_state import TransitionState


def _snap(**overrides) -> MemorySnapshot:
    """Build a snapshot with all-zero defaults plus per-test overrides."""
    base = dict(
        game_mode=0, level_num=0, room_num=0, level_start=0, player_anim=0,
        exit_mode=0, io_port=0, fanfare=0, boss_defeat=0, midway=0, cp_entrance=0,
    )
    base.update(overrides)
    return MemorySnapshot(**base)


def test_death_frame_fires_on_anim_zero_to_nine():
    prev = _snap(player_anim=0)
    curr = _snap(player_anim=9)
    assert is_death_frame(prev, curr) is True


def test_death_frame_does_not_fire_when_already_dying():
    prev = _snap(player_anim=9)
    curr = _snap(player_anim=9)
    assert is_death_frame(prev, curr) is False


def test_exit_frame_fires_on_exit_mode_edge():
    prev = _snap(exit_mode=0)
    curr = _snap(exit_mode=1)
    assert is_exit_frame(prev, curr) is True


def test_exit_frame_does_not_fire_when_exit_mode_unchanged():
    prev = _snap(exit_mode=1)
    curr = _snap(exit_mode=1)
    assert is_exit_frame(prev, curr) is False


def test_goal_type_key():
    assert goal_type(_snap(io_port=a.IO_KEY)) == "key"


def test_goal_type_orb():
    assert goal_type(_snap(io_port=a.IO_ORB)) == "orb"


def test_goal_type_boss():
    assert goal_type(_snap(boss_defeat=1, fanfare=1)) == "boss"


def test_goal_type_normal():
    assert goal_type(_snap(fanfare=1)) == "normal"


def test_goal_type_abort():
    """Default — no fanfare, no goal flag."""
    assert goal_type(_snap()) == "abort"


def test_check_checkpoint_hit_midway():
    """Midway tape: midway 0 -> 1, no goal/orb/key/fadeout."""
    state = TransitionState(first_cp_entrance=0)
    prev = _snap(midway=0)
    curr = _snap(midway=1)
    assert check_checkpoint_hit(prev, curr, state) == "midway"


def test_check_checkpoint_hit_cp_entrance():
    """ASM-style cp_entrance change while in level, distinct from first."""
    state = TransitionState(first_cp_entrance=0x10)
    prev = _snap(level_num=1, cp_entrance=0x10)
    curr = _snap(level_num=1, cp_entrance=0x20)
    assert check_checkpoint_hit(prev, curr, state) == "cp_entrance"


def test_check_checkpoint_hit_suppressed_during_goal():
    """midway hit is ignored if the goal also fired this frame."""
    state = TransitionState(first_cp_entrance=0)
    prev = _snap(midway=0)
    curr = _snap(midway=1, fanfare=1)
    assert check_checkpoint_hit(prev, curr, state) is None


def test_detect_finish_normal_goal():
    prev = _snap(fanfare=0)
    curr = _snap(fanfare=1)
    assert detect_finish(prev, curr) == "normal"


def test_detect_finish_boss():
    prev = _snap(fanfare=0, boss_defeat=0)
    curr = _snap(fanfare=1, boss_defeat=1)
    assert detect_finish(prev, curr) == "boss"


def test_detect_finish_orb():
    prev = _snap(io_port=0)
    curr = _snap(io_port=a.IO_ORB)
    assert detect_finish(prev, curr) == "orb"


def test_detect_finish_key():
    prev = _snap(io_port=0)
    curr = _snap(io_port=a.IO_KEY)
    assert detect_finish(prev, curr) == "key"


def test_detect_finish_none_when_static():
    """No transitions → no finish event."""
    snap = _snap(fanfare=1)
    assert detect_finish(snap, snap) is None
