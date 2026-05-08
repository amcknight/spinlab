"""Tests for segment_id_for_event — the single source of truth for save-state
file naming under SpinLab. Both StateIO.segment_id_for_event and the capture
controllers' save-on-event hooks delegate here."""
from spinlab.capture.segment_naming import segment_id_for_event
from spinlab.protocol import (
    CheckpointEvent,
    DeathEvent,
    LevelEntranceEvent,
    LevelExitEvent,
    SpawnEvent,
)


def test_entrance_id():
    ev = LevelEntranceEvent(level=5, room=2)
    assert segment_id_for_event(ev) == "entrance_5_2"


def test_checkpoint_hot_id():
    ev = CheckpointEvent(level_num=5, cp_ordinal=1)
    assert segment_id_for_event(ev) == "cp_5_1_hot"


def test_spawn_with_segment_id_passes_through():
    ev = SpawnEvent(level_num=5, segment_id="my_seg_id")
    assert segment_id_for_event(ev) == "my_seg_id"


def test_spawn_without_segment_id_returns_none():
    ev = SpawnEvent(level_num=5)
    assert segment_id_for_event(ev) is None


def test_death_event_returns_none():
    assert segment_id_for_event(DeathEvent()) is None


def test_level_exit_returns_none():
    """LevelExit has no associated state file; resolver returns None."""
    assert segment_id_for_event(LevelExitEvent(level=5, goal="goal")) is None
