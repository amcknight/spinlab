"""Tests for the typed event/command catalog (dataclass shape)."""
from spinlab.protocol import (
    CheckpointEvent,
    DeathEvent,
    LevelEntranceEvent,
    LevelExitEvent,
    SpawnEvent,
)


class TestRichEventFields:
    """Protocol events carry the rich fields the RA detector emits.

    Added in 2026-05-07's event-pipeline-collapse refactor — the previous
    split between internal retroarch.events.* and wire-shape protocol.*
    classes dropped fields like room/elapsed_ms/segment_id at the boundary.
    """

    def test_level_entrance_event_carries_room_and_frame(self):
        ev = LevelEntranceEvent(level=5, room=2, frame=120)
        assert ev.room == 2
        assert ev.frame == 120

    def test_level_exit_event_carries_room_elapsed_frame(self):
        ev = LevelExitEvent(level=5, room=1, goal="goal", elapsed_ms=12345, frame=600)
        assert ev.room == 1
        assert ev.elapsed_ms == 12345
        assert ev.frame == 600

    def test_checkpoint_event_carries_cp_type(self):
        ev = CheckpointEvent(level_num=5, cp_ordinal=1, cp_type="midway")
        assert ev.cp_type == "midway"

    def test_spawn_event_carries_segment_id(self):
        ev = SpawnEvent(level_num=5, segment_id="seg_abc", is_cold_cp=True)
        assert ev.segment_id == "seg_abc"

    def test_death_event_carries_level_num_and_timestamp(self):
        ev = DeathEvent(level_num=7, timestamp_ms=999)
        assert ev.level_num == 7
        assert ev.timestamp_ms == 999
