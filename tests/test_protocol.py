# tests/test_protocol.py
"""Tests for the typed TCP protocol — message catalog and parsing."""
import json

import pytest

from spinlab.protocol import (
    AttemptInvalidatedEvent,
    AttemptResultEvent,
    CheckpointEvent,
    ColdFillLoadCmd,
    DeathEvent,
    FillGapLoadCmd,
    GameContextCmd,
    GameContextEvent,
    LevelEntranceEvent,
    LevelExitEvent,
    PracticeLoadCmd,
    PracticeStopCmd,
    RecSavedEvent,
    ReferenceStartCmd,
    ReferenceStopCmd,
    ReplayCmd,
    ReplayErrorEvent,
    ReplayFinishedEvent,
    ReplayProgressEvent,
    ReplayStartedEvent,
    RomInfoEvent,
    SetConditionsCmd,
    SetInvalidateComboCmd,
    SpawnEvent,
    parse_event,
    serialize_command,
)


class TestParseEvent:
    def test_parse_rom_info(self):
        raw = {"event": "rom_info", "filename": "test.sfc"}
        evt = parse_event(raw)
        assert isinstance(evt, RomInfoEvent)
        assert evt.filename == "test.sfc"

    def test_parse_spawn_with_conditions(self):
        raw = {
            "event": "spawn",
            "level_num": 105,
            "state_captured": True,
            "state_path": "/cold.mss",
            "conditions": {"powerup": 2},
            "is_cold_cp": True,
            "cp_ordinal": 1,
        }
        evt = parse_event(raw)
        assert isinstance(evt, SpawnEvent)
        assert evt.level_num == 105
        assert evt.state_captured is True
        assert evt.conditions == {"powerup": 2}

    def test_parse_death(self):
        evt = parse_event({"event": "death"})
        assert isinstance(evt, DeathEvent)

    def test_parse_attempt_result(self):
        raw = {
            "event": "attempt_result",
            "segment_id": "seg1",
            "completed": True,
            "time_ms": 5000,
            "deaths": 0,
            "clean_tail_ms": 5000,
        }
        evt = parse_event(raw)
        assert isinstance(evt, AttemptResultEvent)
        assert evt.segment_id == "seg1"
        assert evt.completed is True
        assert evt.time_ms == 5000

    def test_parse_level_exit(self):
        raw = {"event": "level_exit", "level": 105, "goal": "normal"}
        evt = parse_event(raw)
        assert isinstance(evt, LevelExitEvent)
        assert evt.level == 105
        assert evt.goal == "normal"

    def test_unknown_event_raises(self):
        with pytest.raises(ValueError, match="Unknown event type"):
            parse_event({"event": "bogus_event"})

    def test_missing_event_field_raises(self):
        with pytest.raises(ValueError, match="Missing 'event' field"):
            parse_event({"not_event": "foo"})

    def test_extra_fields_ignored(self):
        raw = {"event": "death", "unexpected_field": 42}
        evt = parse_event(raw)
        assert isinstance(evt, DeathEvent)


class TestSerializeCommand:
    def test_reference_start(self):
        cmd = ReferenceStartCmd(path="/rec/run.spinrec")
        msg = serialize_command(cmd)
        parsed = json.loads(msg)
        assert parsed["event"] == "reference_start"
        assert parsed["path"] == "/rec/run.spinrec"

    def test_reference_stop(self):
        msg = serialize_command(ReferenceStopCmd())
        parsed = json.loads(msg)
        assert parsed["event"] == "reference_stop"

    def test_set_conditions(self):
        cmd = SetConditionsCmd(definitions=[
            {"name": "powerup", "address": 25, "size": 1},
        ])
        msg = serialize_command(cmd)
        parsed = json.loads(msg)
        assert parsed["event"] == "set_conditions"
        assert len(parsed["definitions"]) == 1

    def test_practice_load(self):
        cmd = PracticeLoadCmd(
            id="seg1", state_path="/state.mss",
            description="L105 start > goal", end_type="goal",
            expected_time_ms=5000, auto_advance_delay_ms=1000,
        )
        msg = serialize_command(cmd)
        parsed = json.loads(msg)
        assert parsed["event"] == "practice_load"
        assert parsed["id"] == "seg1"
        assert parsed["state_path"] == "/state.mss"

    def test_practice_stop(self):
        msg = serialize_command(PracticeStopCmd())
        parsed = json.loads(msg)
        assert parsed["event"] == "practice_stop"
