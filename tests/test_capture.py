"""Tests for capture.py JSONL parsing and event pairing."""
import pytest
from spinlab.capture import build_manifest, pair_events, parse_log


def entrance(level, room, frame=0, state_path="states/x.mss"):
    return {
        "event": "level_entrance",
        "level": level,
        "room": room,
        "frame": frame,
        "ts_ms": 0,
        "session": "passive",
        "state_path": state_path,
    }


def exit_event(level, room, goal="normal", elapsed_ms=5000):
    return {
        "event": "level_exit",
        "level": level,
        "room": room,
        "goal": goal,
        "elapsed_ms": elapsed_ms,
        "frame": 0,
        "ts_ms": 0,
        "session": "passive",
    }


def test_parse_log_returns_list_of_dicts():
    lines = ['{"event": "death", "level": 105}', "", '{"event": "level_exit", "level": 106}']
    result = parse_log(lines)
    assert len(result) == 2
    assert result[0]["event"] == "death"


def test_pair_events_basic():
    events = [entrance(105, 1), exit_event(105, 1, "normal", 5000)]
    pairs = pair_events(events)
    assert len(pairs) == 1
    e, x = pairs[0]
    assert e["level"] == 105
    assert x["goal"] == "normal"
    assert x["elapsed_ms"] == 5000


def test_pair_events_two_levels():
    events = [
        entrance(105, 1),
        exit_event(105, 1, "normal", 3000),
        entrance(106, 1),
        exit_event(106, 1, "key", 8000),
    ]
    pairs = pair_events(events)
    assert len(pairs) == 2
    assert pairs[0][1]["elapsed_ms"] == 3000
    assert pairs[1][1]["goal"] == "key"


def test_pair_events_entrance_with_no_exit_is_dropped():
    events = [entrance(105, 1)]
    pairs = pair_events(events)
    assert len(pairs) == 0


def test_pair_events_death_between_entrance_and_exit_ignored():
    """Deaths are not entrance/exit events, so pairing ignores them."""
    events = [
        entrance(105, 1),
        {"event": "death", "level": 105, "room": 1},
        exit_event(105, 1, "normal", 4000),
    ]
    pairs = pair_events(events)
    assert len(pairs) == 1


def test_build_manifest_structure():
    pairs = [
        (entrance(105, 1, state_path="C:/states/smw_cod_105_1.mss"),
         exit_event(105, 1, "normal", 5000)),
    ]
    manifest = build_manifest(pairs, game_id="smw_cod", category="any%")
    assert manifest["game_id"] == "smw_cod"
    assert manifest["category"] == "any%"
    assert "captured_at" in manifest
    assert len(manifest["splits"]) == 1


def test_build_manifest_split_fields():
    pairs = [
        (entrance(105, 1, state_path="C:/states/smw_cod_105_1.mss"),
         exit_event(105, 1, "key", 8100)),
    ]
    manifest = build_manifest(pairs, game_id="smw_cod", category="any%")
    split = manifest["splits"][0]
    assert split["id"] == "smw_cod:105:1:key"
    assert split["level_number"] == 105
    assert split["room_id"] == 1
    assert split["goal"] == "key"
    assert split["state_path"] == "C:/states/smw_cod_105_1.mss"
    assert split["reference_time_ms"] == 8100
