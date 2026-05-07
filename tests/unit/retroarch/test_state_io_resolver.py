"""StateIO path-resolution and has_state_for tests. No NCI involvement here."""
from pathlib import Path

import pytest

from spinlab.retroarch.events import (
    Checkpoint,
    Death,
    LevelEntrance,
    LevelExit,
    Spawn,
)
from spinlab.retroarch.state_io import StateIO


class _FakeClient:
    """Stub NCIClient — none of these tests hit the wire."""


@pytest.fixture
def state_io(tmp_path):
    """Build StateIO against fresh tmp dirs. RA dir intentionally empty."""
    ra_dir = tmp_path / "ra_savestates"
    ra_dir.mkdir()
    sl_dir = tmp_path / "spinlab_states"
    sl_dir.mkdir()
    return StateIO(
        client=_FakeClient(),
        ra_savestate_dir=ra_dir,
        spinlab_state_dir=sl_dir,
        ra_game_basename="Test Game",
    )


def test_state_path_for_returns_keyed_path(state_io, tmp_path):
    p = state_io.state_path_for("seg-abc")
    assert p == tmp_path / "spinlab_states" / "seg-abc.state"


def test_state_path_for_sanitizes_segment_id(state_io, tmp_path):
    p = state_io.state_path_for("game:5:cp1")
    assert p == tmp_path / "spinlab_states" / "game_5_cp1.state"


def test_has_state_for_false_when_missing(state_io):
    assert state_io.has_state_for("seg-abc") is False


def test_has_state_for_true_after_file_created(state_io, tmp_path):
    f = tmp_path / "spinlab_states" / "seg-abc.state"
    f.write_bytes(b"x")
    assert state_io.has_state_for("seg-abc") is True


# resolve_event_path tests: per-event-type behaviour.

def test_resolve_event_path_level_entrance_uses_level_room(state_io, tmp_path):
    """LevelEntrance state path keyed by level+room (no segment_id known yet)."""
    ev = LevelEntrance(timestamp_ms=0, level=5, room=0)
    p = state_io.resolve_event_path(ev)
    assert p.endswith("entrance_5_0.state")


def test_resolve_event_path_checkpoint_uses_level_ordinal(state_io, tmp_path):
    ev = Checkpoint(timestamp_ms=0, level_num=5, cp_type="midway", cp_ordinal=2)
    p = state_io.resolve_event_path(ev)
    assert p.endswith("cp_5_2_hot.state")


def test_resolve_event_path_spawn_uses_segment_id(state_io, tmp_path):
    """Cold-fill spawn carries its own segment_id."""
    ev = Spawn(timestamp_ms=0, level_num=5, segment_id="seg-cold-1",
               state_captured=True, is_cold_cp=True)
    p = state_io.resolve_event_path(ev)
    assert p.endswith("seg-cold-1.state")


def test_resolve_event_path_spawn_without_segment_id_returns_empty(state_io):
    """Defensive: if for some reason segment_id is unset, return '' (no path)."""
    ev = Spawn(timestamp_ms=0, level_num=5, segment_id="")
    assert state_io.resolve_event_path(ev) == ""


def test_resolve_event_path_death_returns_empty(state_io):
    """Death has no state_path field — resolver returns ''."""
    assert state_io.resolve_event_path(Death(timestamp_ms=0)) == ""


def test_resolve_event_path_level_exit_returns_empty(state_io):
    """LevelExit isn't path-tagged — that's per the Lua audit."""
    ev = LevelExit(timestamp_ms=0, level=5, goal="normal")
    assert state_io.resolve_event_path(ev) == ""
