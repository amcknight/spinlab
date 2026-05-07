"""TransitionDetector tests — drive sequences of synthetic snapshots through it."""
from spinlab.retroarch import addresses as a
from spinlab.retroarch.detector import TransitionDetector
from spinlab.retroarch.events import (
    Checkpoint,
    Death,
    LevelEntrance,
    LevelExit,
    Spawn,
)
from spinlab.retroarch.snapshot import MemorySnapshot


def _snap(**overrides) -> MemorySnapshot:
    base = dict(
        game_mode=0, level_num=0, room_num=0, level_start=0, player_anim=0,
        exit_mode=0, io_port=0, fanfare=0, boss_defeat=0, midway=0, cp_entrance=0,
    )
    base.update(overrides)
    return MemorySnapshot(**base)


def test_initial_step_emits_no_events():
    d = TransitionDetector()
    events = d.step(_snap(), timestamp_ms=0)
    assert events == []


def test_level_entrance_on_level_start_edge():
    d = TransitionDetector()
    d.step(_snap(level_num=5), timestamp_ms=0)  # prev seeded
    events = d.step(_snap(level_num=5, level_start=1), timestamp_ms=16)

    assert len(events) == 1
    assert isinstance(events[0], LevelEntrance)
    assert events[0].level == 5


def test_death_emits_once_per_anim_transition():
    d = TransitionDetector()
    d.step(_snap(player_anim=0), timestamp_ms=0)
    e1 = d.step(_snap(player_anim=9), timestamp_ms=16)
    e2 = d.step(_snap(player_anim=9), timestamp_ms=32)

    assert any(isinstance(e, Death) for e in e1)
    assert not any(isinstance(e, Death) for e in e2), "death must not refire while still dying"


def test_exit_emits_on_exit_mode_edge():
    d = TransitionDetector()
    d.step(_snap(exit_mode=0, fanfare=1, level_num=5), timestamp_ms=0)
    events = d.step(_snap(exit_mode=1, fanfare=1, level_num=5), timestamp_ms=16)

    assert any(isinstance(e, LevelExit) and e.goal == "normal" for e in events)


def test_checkpoint_then_spawn_after_death():
    """Real sequence: hit midway -> die -> respawn."""
    d = TransitionDetector()
    # Frame 1: clean snapshot.
    d.step(_snap(level_num=5, midway=0, level_start=1), timestamp_ms=0)
    # Frame 2: midway tape.
    cp_events = d.step(_snap(level_num=5, midway=1, level_start=1), timestamp_ms=16)
    assert any(isinstance(e, Checkpoint) and e.cp_type == "midway" for e in cp_events)
    # Frame 3: death.
    d.step(_snap(level_num=5, midway=1, player_anim=9, level_start=1), timestamp_ms=32)
    # Frame 4: still dying.
    d.step(_snap(level_num=5, midway=1, player_anim=9, level_start=0), timestamp_ms=48)
    # Frame 5: respawn — level_start 0 -> 1 with died_flag still set.
    spawn_events = d.step(_snap(level_num=5, midway=1, level_start=1), timestamp_ms=64)
    assert any(isinstance(e, Spawn) and e.is_cold_cp for e in spawn_events)
