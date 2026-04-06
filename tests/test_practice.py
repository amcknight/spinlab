"""Tests for the async practice loop."""
import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock
from pathlib import Path

from spinlab.db import Database
from spinlab.models import Segment, Waypoint, WaypointSaveState
from spinlab.practice import PracticeSession
from spinlab.scheduler import Scheduler


def _make_seg_with_state(db, game_id, level, start_type, end_type,
                         state_path, ordinal=1):
    """Create waypoints + segment + hot save state; return segment."""
    wp_start = Waypoint.make(game_id, level, start_type, 0, {})
    wp_end = Waypoint.make(game_id, level, end_type, 0, {})
    db.upsert_waypoint(wp_start)
    db.upsert_waypoint(wp_end)
    seg = Segment(
        id=Segment.make_id(game_id, level, start_type, 0, end_type, 0,
                           wp_start.id, wp_end.id),
        game_id=game_id, level_number=level,
        start_type=start_type, start_ordinal=0,
        end_type=end_type, end_ordinal=0,
        description="L1" if start_type == "entrance" else "",
        ordinal=ordinal,
        start_waypoint_id=wp_start.id, end_waypoint_id=wp_end.id,
    )
    db.upsert_segment(seg)
    db.add_save_state(WaypointSaveState(
        waypoint_id=wp_start.id, variant_type="hot",
        state_path=str(state_path), is_default=True,
    ))
    return seg


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    d.upsert_game("g", "Game", "any%")
    state_file = tmp_path / "test.mss"
    state_file.write_bytes(b"fake state")
    seg = _make_seg_with_state(d, "g", 1, "entrance", "goal", state_file)
    d._test_seg_id = seg.id
    d._test_state_file = state_file
    return d


@pytest.mark.asyncio
async def test_practice_session_picks_and_sends(db):
    """Practice session should pick a segment and send practice_load."""
    seg_id = db._test_seg_id
    mock_tcp = AsyncMock()
    mock_tcp.is_connected = True
    mock_tcp.send = AsyncMock()

    # Simulate receiving an attempt_result after send
    result_event = {
        "event": "attempt_result",
        "segment_id": seg_id,
        "completed": True,
        "time_ms": 4500,
    }

    session = PracticeSession(tcp=mock_tcp, db=db, game_id="g")
    session.is_running = True

    # Deliver result via receive_result after a short delay
    async def deliver():
        await asyncio.sleep(0.05)
        session.receive_result(result_event)

    asyncio.create_task(deliver())
    await session.run_one()

    # Verify practice_load was sent
    mock_tcp.send.assert_called_once()
    sent = mock_tcp.send.call_args[0][0]
    assert sent.startswith("practice_load:")

    # Verify attempt was logged
    attempts = db.get_segment_attempts(seg_id)
    assert len(attempts) == 1
    assert attempts[0]["completed"] == 1


@pytest.mark.asyncio
async def test_practice_session_state(db):
    session = PracticeSession(tcp=AsyncMock(), db=db, game_id="g")
    assert session.is_running is False
    assert session.current_segment_id is None
    assert session.segments_attempted == 0


class TestReceiveResult:
    @pytest.mark.asyncio
    async def test_receive_result_unblocks_run_one(self, tmp_path):
        """run_one awaits asyncio.Event, receive_result sets it."""
        tcp = MagicMock()
        tcp.is_connected = True
        tcp.send = AsyncMock()
        db = MagicMock()
        db.create_session = MagicMock()
        db.end_session = MagicMock()
        db.log_attempt = MagicMock()
        db.load_allocator_config = MagicMock(return_value=None)
        db.get_all_segments_with_model = MagicMock(return_value=[])
        db.load_model_state = MagicMock(return_value=None)
        db.save_model_state = MagicMock()

        ps = PracticeSession(tcp=tcp, db=db, game_id="test")
        ps.is_running = True

        # Create a real state file so os.path.exists passes
        state_file = tmp_path / "test.mss"
        state_file.write_bytes(b"fake state")

        # Simulate scheduler returning a segment
        mock_segment = MagicMock()
        mock_segment.segment_id = "s1"
        mock_segment.state_path = str(state_file)
        mock_segment.end_type = "goal"
        mock_segment.description = "Test"
        mock_segment.estimator_state = None
        mock_segment.model_outputs = {}
        mock_segment.selected_model = "kalman"

        ps.scheduler.pick_next = MagicMock(return_value=mock_segment)
        ps.scheduler.process_attempt = MagicMock()

        # Schedule receive_result after a short delay
        async def deliver_result():
            await asyncio.sleep(0.1)
            ps.receive_result({
                "event": "attempt_result",
                "segment_id": "s1",
                "completed": True,
                "time_ms": 4500,
            })

        asyncio.create_task(deliver_result())
        result = await ps.run_one()

        assert result is True
        assert ps.segments_completed == 1


def test_snapshot_expected_times_at_start(db):
    """start() should populate initial_expected_total_ms and _clean_ms
    with the sum of expected_ms across practicable segments."""
    seg_id = db._test_seg_id
    # Seed an attempt so the estimator produces an expected_ms.
    sched = Scheduler(db, "g")
    sched.process_attempt(seg_id, time_ms=5000, completed=True, deaths=0)

    tcp = AsyncMock()
    tcp.is_connected = True
    ps = PracticeSession(tcp=tcp, db=db, game_id="g")
    ps.start()

    assert ps.initial_expected_total_ms is not None
    assert ps.initial_expected_total_ms > 0
    # clean_tail_ms was not supplied but completed+deaths=0 implies it equals time_ms
    assert ps.initial_expected_clean_ms is not None
    assert ps.initial_expected_clean_ms > 0


def test_snapshot_skips_segments_without_state_path(db, tmp_path):
    """Segments whose state_path does not exist on disk are excluded."""
    seg_id = db._test_seg_id
    # Add a second segment with no waypoint save state -> state_path = None
    wp_start2 = Waypoint.make("g", 2, "entrance", 0, {"n": "2"})
    wp_end2 = Waypoint.make("g", 2, "goal", 0, {"n": "2"})
    db.upsert_waypoint(wp_start2)
    db.upsert_waypoint(wp_end2)
    seg2_id = Segment.make_id("g", 2, "entrance", 0, "goal", 0,
                              wp_start2.id, wp_end2.id)
    seg2 = Segment(
        id=seg2_id, game_id="g", level_number=2,
        start_type="entrance", start_ordinal=0,
        end_type="goal", end_ordinal=0,
        description="L2", ordinal=2,
        start_waypoint_id=wp_start2.id, end_waypoint_id=wp_end2.id,
    )
    db.upsert_segment(seg2)
    # No save state for wp_start2 => state_path will be NULL

    # Seed attempts on BOTH segments so they each have estimates.
    sched = Scheduler(db, "g")
    sched.process_attempt(seg_id, time_ms=5000, completed=True, deaths=0)
    sched.process_attempt(seg2_id, time_ms=8000, completed=True, deaths=0)

    tcp = AsyncMock()
    tcp.is_connected = True
    ps = PracticeSession(tcp=tcp, db=db, game_id="g")
    ps.start()

    # Only seg_id had a real state_path; seg2 contributes nothing.
    # The sum should reflect only seg_id's expected_ms (~5000).
    assert ps.initial_expected_total_ms is not None
    assert ps.initial_expected_total_ms < 6000


def test_snapshot_all_missing_returns_none(db):
    """When no segment has estimates at session start, both snapshots are None."""
    tcp = AsyncMock()
    tcp.is_connected = True
    # No process_attempt call -> no model state -> no expected_ms
    ps = PracticeSession(tcp=tcp, db=db, game_id="g")
    ps.start()

    assert ps.initial_expected_total_ms is None
    assert ps.initial_expected_clean_ms is None


def test_current_expected_times_reflects_model_updates(db):
    """After process_attempt runs, current_expected_times() returns the new sum."""
    seg_id = db._test_seg_id
    sched = Scheduler(db, "g")
    sched.process_attempt(seg_id, time_ms=5000, completed=True, deaths=0)

    tcp = AsyncMock()
    tcp.is_connected = True
    ps = PracticeSession(tcp=tcp, db=db, game_id="g")
    ps.start()
    initial_total = ps.initial_expected_total_ms

    # Simulate a faster attempt pulling the estimate down.
    ps.scheduler.process_attempt(seg_id, time_ms=3000, completed=True, deaths=0)

    cur_total, cur_clean = ps.current_expected_times()
    assert cur_total is not None
    assert cur_total < initial_total
