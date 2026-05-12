"""Tests for FillGapController — extracted from ReferenceController."""
from __future__ import annotations

import pytest

from spinlab.capture.fill_gap import FillGapController
from spinlab.db import Database
from spinlab.errors import NoHotVariantError, NotConnectedError
from spinlab.models import Mode, Status, Waypoint, Segment
from spinlab.protocol import FillGapLoadCmd, SpawnEvent

from tests.conftest import FakeEmuBackend, make_seg_with_state


@pytest.fixture
def fg_db(tmp_path):
    """Database with no segments — for error-path tests."""
    db = Database(tmp_path / "fg.db")
    db.upsert_game("g1", "Test", "any%")
    return db


@pytest.fixture
def fg_db_with_hot(tmp_path):
    """Database with one entrance→goal segment that has a hot save state."""
    db = Database(tmp_path / "fg_hot.db")
    db.upsert_game("g1", "Test", "any%")
    state_file = tmp_path / "seg1_hot.mss"
    state_file.write_bytes(b"")
    seg = make_seg_with_state(db, "g1", 1, "entrance", "goal", state_file)
    db._seg_id = seg.id
    db._wp_id = seg.start_waypoint_id
    return db


@pytest.fixture
def fg_db_without_hot(tmp_path):
    """Database with a segment whose start waypoint has no hot variant."""
    db = Database(tmp_path / "fg_nohot.db")
    db.upsert_game("g1", "Test", "any%")
    # Build the segment but skip the WaypointSaveState insert.
    wp_start = Waypoint.make("g1", 1, "entrance", 0, {})
    wp_end = Waypoint.make("g1", 1, "goal", 0, {})
    db.upsert_waypoint(wp_start)
    db.upsert_waypoint(wp_end)
    seg = Segment(
        id=Segment.make_id("g1", 1, "entrance", 0, "goal", 0, wp_start.id, wp_end.id),
        game_id="g1", level_number=1,
        start_type="entrance", start_ordinal=0,
        end_type="goal", end_ordinal=0,
        description="L1", ordinal=1,
        start_waypoint_id=wp_start.id, end_waypoint_id=wp_end.id,
    )
    db.upsert_segment(seg)
    db._seg_id = seg.id
    return db


@pytest.fixture
def fg_emu():
    return FakeEmuBackend(connected=True)


@pytest.mark.asyncio
async def test_start_raises_when_not_connected(fg_db, fg_emu):
    fg_emu.is_connected = False
    fg = FillGapController(fg_db, fg_emu)
    with pytest.raises(NotConnectedError):
        await fg.start("any-segment-id")


@pytest.mark.asyncio
async def test_start_raises_no_hot_variant(fg_db_without_hot, fg_emu):
    fg = FillGapController(fg_db_without_hot, fg_emu)
    with pytest.raises(NoHotVariantError):
        await fg.start(fg_db_without_hot._seg_id)


@pytest.mark.asyncio
async def test_start_happy_path(fg_db_with_hot, fg_emu):
    fg = FillGapController(fg_db_with_hot, fg_emu)
    result = await fg.start(fg_db_with_hot._seg_id)
    assert result.status == Status.STARTED
    assert result.new_mode == Mode.FILL_GAP
    assert fg.is_active is True
    assert fg.segment_id == fg_db_with_hot._seg_id
    assert len(fg_emu.sent_commands) == 1
    assert isinstance(fg_emu.sent_commands[0], FillGapLoadCmd)


def test_handle_spawn_returns_false_when_inactive(fg_db, fg_emu):
    fg = FillGapController(fg_db, fg_emu)
    assert fg.handle_spawn(SpawnEvent(state_path="/c.mss")) is False
    assert fg.is_active is False


def test_handle_spawn_returns_false_when_no_state_path(fg_db_with_hot, fg_emu):
    fg = FillGapController(fg_db_with_hot, fg_emu)
    fg._segment_id = "seg1"
    fg._waypoint_id = "wp1"
    assert fg.handle_spawn(SpawnEvent(state_path=None)) is False
    assert fg.is_active is True, "state should be preserved — event not consumed"


@pytest.mark.asyncio
async def test_handle_spawn_happy_path_persists_cold(fg_db_with_hot, fg_emu):
    fg = FillGapController(fg_db_with_hot, fg_emu)
    await fg.start(fg_db_with_hot._seg_id)
    consumed = fg.handle_spawn(SpawnEvent(state_path="/cold1.mss"))
    assert consumed is True
    assert fg.is_active is False
    cold = fg_db_with_hot.get_save_state(fg_db_with_hot._wp_id, "cold")
    assert cold is not None
    assert cold.state_path == "/cold1.mss"


def test_clear_resets_state(fg_db, fg_emu):
    fg = FillGapController(fg_db, fg_emu)
    fg._segment_id = "seg1"
    fg._waypoint_id = "wp1"
    fg.clear()
    assert fg.is_active is False
    assert fg.segment_id is None
