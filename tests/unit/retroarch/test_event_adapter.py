"""Tests for the TransitionEvent -> protocol-dict adapter."""
import pytest

from spinlab.protocol import (
    CheckpointEvent,
    DeathEvent,
    LevelEntranceEvent,
    LevelExitEvent,
    RomInfoEvent,
    SpawnEvent,
    parse_event,
)
from spinlab.retroarch.event_adapter import to_protocol_dict, to_rom_info_dict
from spinlab.retroarch.events import (
    Checkpoint,
    Death,
    LevelEntrance,
    LevelExit,
    Spawn,
    TransitionEvent,
)
from spinlab.retroarch.responses import StatusInfo


# --- LevelEntrance --------------------------------------------------------

def test_level_entrance_basic_fields():
    ev = LevelEntrance(
        level=5, room=2, frame=120,
        state_path="/p/seg.state",
        timestamp_ms=1000,
        conditions={"x": 1},
    )
    d = to_protocol_dict(ev)
    assert d["event"] == "level_entrance"
    assert d["level"] == 5
    assert d["state_path"] == "/p/seg.state"
    assert d["timestamp_ms"] == 1000
    assert d["conditions"] == {"x": 1}


def test_level_entrance_empty_state_path_becomes_none():
    """Phase C uses '' for unset; protocol uses None."""
    ev = LevelEntrance(level=5, room=0, frame=0, state_path="", timestamp_ms=0)
    d = to_protocol_dict(ev)
    assert d["state_path"] is None


def test_level_entrance_roundtrips_through_parse_event():
    ev = LevelEntrance(level=5, room=2, frame=120, state_path="/p/seg.state",
                       timestamp_ms=1000, conditions={"x": 1})
    parsed = parse_event(to_protocol_dict(ev))
    assert isinstance(parsed, LevelEntranceEvent)
    assert parsed.level == 5
    assert parsed.state_path == "/p/seg.state"


# --- Death ---------------------------------------------------------------

def test_death_emits_only_event_marker():
    """Lua's DeathEvent has no payload."""
    ev = Death(level_num=5, timestamp_ms=2000, conditions={"a": 9})
    d = to_protocol_dict(ev)
    assert d == {"event": "death"}


def test_death_roundtrips_through_parse_event():
    parsed = parse_event(to_protocol_dict(Death(timestamp_ms=0)))
    assert isinstance(parsed, DeathEvent)


# --- LevelExit -----------------------------------------------------------

def test_level_exit_drops_room_elapsed_frame():
    ev = LevelExit(
        level=5, room=3, goal="normal", elapsed_ms=12000, frame=720,
        timestamp_ms=3000, conditions={},
    )
    d = to_protocol_dict(ev)
    assert d["event"] == "level_exit"
    assert d["level"] == 5
    assert d["goal"] == "normal"
    assert d["timestamp_ms"] == 3000
    assert "room" not in d
    assert "elapsed_ms" not in d
    assert "frame" not in d


def test_level_exit_roundtrips_through_parse_event():
    ev = LevelExit(level=5, room=0, goal="orb", elapsed_ms=0, frame=0, timestamp_ms=0)
    parsed = parse_event(to_protocol_dict(ev))
    assert isinstance(parsed, LevelExitEvent)
    assert parsed.goal == "orb"


# --- Checkpoint ----------------------------------------------------------

def test_checkpoint_drops_cp_type():
    ev = Checkpoint(
        level_num=5, cp_type="midway", cp_ordinal=2,
        state_path="/p/cp.state", timestamp_ms=4000,
    )
    d = to_protocol_dict(ev)
    assert d["event"] == "checkpoint"
    assert d["level_num"] == 5
    assert d["cp_ordinal"] == 2
    assert d["state_path"] == "/p/cp.state"
    assert "cp_type" not in d


def test_checkpoint_empty_state_path_becomes_none():
    ev = Checkpoint(level_num=5, cp_type="midway", cp_ordinal=1,
                   state_path="", timestamp_ms=0)
    d = to_protocol_dict(ev)
    assert d["state_path"] is None


def test_checkpoint_roundtrips_through_parse_event():
    ev = Checkpoint(level_num=5, cp_type="midway", cp_ordinal=1,
                   state_path="/x.state", timestamp_ms=0)
    parsed = parse_event(to_protocol_dict(ev))
    assert isinstance(parsed, CheckpointEvent)
    assert parsed.cp_ordinal == 1


# --- Spawn ---------------------------------------------------------------

def test_spawn_drops_segment_id():
    """segment_id is orchestrator-internal; not in protocol."""
    ev = Spawn(
        level_num=5, is_cold_cp=True, cp_ordinal=2, state_captured=True,
        state_path="/p/spawn.state", segment_id="seg-x",
        timestamp_ms=5000, conditions={},
    )
    d = to_protocol_dict(ev)
    assert d["event"] == "spawn"
    assert d["level_num"] == 5
    assert d["is_cold_cp"] is True
    assert d["cp_ordinal"] == 2
    assert d["state_captured"] is True
    assert d["state_path"] == "/p/spawn.state"
    assert "segment_id" not in d


def test_spawn_empty_state_path_becomes_none():
    ev = Spawn(level_num=5, is_cold_cp=False, cp_ordinal=0, state_captured=False,
               state_path="", segment_id="", timestamp_ms=0)
    d = to_protocol_dict(ev)
    assert d["state_path"] is None


def test_spawn_roundtrips_through_parse_event():
    ev = Spawn(level_num=5, is_cold_cp=True, cp_ordinal=2, state_captured=True,
               state_path="/p.state", segment_id="seg-x", timestamp_ms=0)
    parsed = parse_event(to_protocol_dict(ev))
    assert isinstance(parsed, SpawnEvent)
    assert parsed.is_cold_cp is True


# --- Unknown subclass guards ---------------------------------------------

def test_unsupported_event_raises_typeerror():
    """Defensive: future TransitionEvent subclasses must update the adapter."""
    class MysteryEvent(TransitionEvent):
        pass

    with pytest.raises(TypeError, match="MysteryEvent"):
        to_protocol_dict(MysteryEvent(timestamp_ms=0))


# --- RomInfo helper ------------------------------------------------------

def test_to_rom_info_dict_uses_game_field():
    s = StatusInfo(state="PAUSED", system="Super Nintendo Entertainment System",
                   game="Toothpaste World", crc32="deadbeef")
    d = to_rom_info_dict(s)
    assert d == {"event": "rom_info", "filename": "Toothpaste World"}
    assert isinstance(parse_event(d), RomInfoEvent)


def test_to_rom_info_dict_handles_missing_game():
    """If status has no game, filename becomes ''."""
    s = StatusInfo(state="STOPPED")
    d = to_rom_info_dict(s)
    assert d == {"event": "rom_info", "filename": ""}
