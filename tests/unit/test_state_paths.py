"""Tests for spinlab.state_paths — segment_id ↔ filename and event resolver."""
from pathlib import Path

import pytest

from spinlab.protocol import (
    CheckpointEvent,
    DeathEvent,
    LevelEntranceEvent,
    LevelExitEvent,
    SpawnEvent,
)
from spinlab.state_paths import (
    StatePathResolver,
    segment_id_for_event,
    segment_state_filename,
)

# --- segment_state_filename ---

def test_segment_state_filename_basic():
    assert segment_state_filename("seg-abc123") == "seg-abc123.state"


def test_segment_state_filename_sanitizes_separators():
    assert segment_state_filename("game:5:cp1") == "game_5_cp1.state"
    assert segment_state_filename("foo/bar") == "foo_bar.state"
    assert segment_state_filename("foo\\bar") == "foo_bar.state"


def test_segment_state_filename_rejects_empty():
    with pytest.raises(ValueError, match="empty"):
        segment_state_filename("")


# --- segment_id_for_event ---

def test_entrance_id():
    assert segment_id_for_event(LevelEntranceEvent(level=5, room=2)) == "entrance_5_2"


def test_checkpoint_hot_id():
    assert (
        segment_id_for_event(CheckpointEvent(level_num=5, cp_ordinal=1))
        == "cp_5_1_hot"
    )


def test_spawn_with_segment_id_passes_through():
    assert (
        segment_id_for_event(SpawnEvent(level_num=5, segment_id="my_seg"))
        == "my_seg"
    )


def test_spawn_without_segment_id_returns_none():
    assert segment_id_for_event(SpawnEvent(level_num=5)) is None


def test_death_event_returns_none():
    assert segment_id_for_event(DeathEvent()) is None


def test_level_exit_returns_none():
    assert segment_id_for_event(LevelExitEvent(level=5, goal="normal")) is None


# --- StatePathResolver ---

def test_resolver_path_for(tmp_path: Path):
    r = StatePathResolver(tmp_path)
    assert r.path_for("seg1") == tmp_path / "seg1.state"
    assert r.path_for("game:5:cp1") == tmp_path / "game_5_cp1.state"


def test_resolver_has(tmp_path: Path):
    r = StatePathResolver(tmp_path)
    assert not r.has("seg1")
    (tmp_path / "seg1.state").write_bytes(b"x")
    assert r.has("seg1")


def test_resolver_state_dir_created(tmp_path: Path):
    sub = tmp_path / "nested" / "states"
    assert not sub.exists()
    StatePathResolver(sub)
    assert sub.exists()


def test_resolver_resolve_event_entrance(tmp_path: Path):
    r = StatePathResolver(tmp_path)
    assert (
        r.resolve_event(LevelEntranceEvent(level=3, room=1))
        == str(tmp_path / "entrance_3_1.state")
    )


def test_resolver_resolve_event_no_segment(tmp_path: Path):
    r = StatePathResolver(tmp_path)
    assert r.resolve_event(DeathEvent()) is None
