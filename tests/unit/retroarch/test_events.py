from spinlab.retroarch.events import (
    Checkpoint,
    Death,
    LevelEntrance,
    LevelExit,
    Spawn,
    TransitionEvent,
)


def test_level_entrance_fields():
    e = LevelEntrance(
        level=5, room=0, frame=120, timestamp_ms=2000,
        state_path="states/foo.state", conditions={"game_mode": 14},
    )
    assert isinstance(e, TransitionEvent)
    assert e.level == 5


def test_death_minimal():
    e = Death(level_num=5, timestamp_ms=3000, conditions={})
    assert isinstance(e, TransitionEvent)


def test_level_exit_full():
    e = LevelExit(
        level=5, room=0, goal="normal", elapsed_ms=10500, frame=600,
        timestamp_ms=4000, conditions={},
    )
    assert e.goal == "normal"


def test_checkpoint_full():
    e = Checkpoint(
        level_num=5, cp_type="midway", cp_ordinal=1, timestamp_ms=5000,
        state_path="states/cp.state", conditions={},
    )
    assert e.cp_type == "midway"


def test_spawn_full():
    e = Spawn(
        level_num=5, is_cold_cp=True, cp_ordinal=1, timestamp_ms=6000,
        state_captured=True, state_path="states/cold.state", conditions={},
    )
    assert e.is_cold_cp is True
