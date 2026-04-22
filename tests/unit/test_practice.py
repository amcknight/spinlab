"""Tests for the async practice loop."""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock

from spinlab.models import Segment, Waypoint
from spinlab.practice import PracticeSession
from spinlab.protocol import AttemptResultEvent, PracticeLoadCmd
from spinlab.scheduler import Scheduler


@pytest.mark.asyncio
async def test_practice_session_picks_and_sends(practice_db):
    """Practice session should pick a segment and send practice_load."""
    seg_id = practice_db._test_seg_id
    mock_tcp = AsyncMock()
    mock_tcp.is_connected = True
    mock_tcp.send = AsyncMock()
    mock_tcp.send_command = AsyncMock()

    session = PracticeSession(tcp=mock_tcp, db=practice_db, game_id="g")
    session.is_running = True

    # Deliver result via receive_result after a short delay
    async def deliver():
        await asyncio.sleep(0.05)
        session.receive_result(AttemptResultEvent(
            segment_id=seg_id,
            completed=True,
            time_ms=4500,
        ))

    asyncio.create_task(deliver())
    await session.run_one()

    # Verify practice_load was sent
    mock_tcp.send_command.assert_called_once()
    cmd = mock_tcp.send_command.call_args[0][0]
    assert isinstance(cmd, PracticeLoadCmd)

    # Verify attempt was logged
    attempts = practice_db.get_segment_attempts(seg_id)
    assert len(attempts) == 1
    assert attempts[0]["completed"] == 1


@pytest.mark.asyncio
async def test_practice_session_state(practice_db):
    session = PracticeSession(tcp=AsyncMock(), db=practice_db, game_id="g")
    assert session.is_running is False
    assert session.current_segment_id is None
    assert session.segments_attempted == 0


class TestReceiveResult:
    @pytest.mark.asyncio
    async def test_receive_result_unblocks_run_one(self, practice_db):
        """run_one awaits asyncio.Event, receive_result sets it."""
        tcp = MagicMock()
        tcp.is_connected = True
        tcp.send = AsyncMock()
        tcp.send_command = AsyncMock()

        seg_id = practice_db._test_seg_id

        ps = PracticeSession(tcp=tcp, db=practice_db, game_id="g")
        ps.is_running = True

        # Schedule receive_result after a short delay
        async def deliver_result():
            await asyncio.sleep(0.1)
            ps.receive_result(AttemptResultEvent(
                segment_id=seg_id,
                completed=True,
                time_ms=4500,
            ))

        asyncio.create_task(deliver_result())
        result = await ps.run_one()

        assert result is True
        assert ps.segments_completed == 1


def test_snapshot_expected_times_at_start(practice_db):
    """start() should populate initial_expected_total_ms and _clean_ms
    with the sum of expected_ms across practicable segments."""
    seg_id = practice_db._test_seg_id
    # Seed an attempt so the estimator produces an expected_ms.
    sched = Scheduler(practice_db, "g")
    sched.process_attempt(seg_id, time_ms=5000, completed=True, deaths=0)

    tcp = AsyncMock()
    tcp.is_connected = True
    ps = PracticeSession(tcp=tcp, db=practice_db, game_id="g")
    ps.start()

    assert ps.initial_expected_total_ms is not None
    assert ps.initial_expected_total_ms > 0
    # clean_tail_ms was not supplied but completed+deaths=0 implies it equals time_ms
    assert ps.initial_expected_clean_ms is not None
    assert ps.initial_expected_clean_ms > 0


def test_snapshot_skips_segments_without_state_path(practice_db, tmp_path):
    """Segments whose state_path does not exist on disk are excluded."""
    seg_id = practice_db._test_seg_id
    # Add a second segment with no waypoint save state -> state_path = None
    wp_start2 = Waypoint.make("g", 2, "entrance", 0, {"n": "2"})
    wp_end2 = Waypoint.make("g", 2, "goal", 0, {"n": "2"})
    practice_db.upsert_waypoint(wp_start2)
    practice_db.upsert_waypoint(wp_end2)
    seg2_id = Segment.make_id("g", 2, "entrance", 0, "goal", 0,
                              wp_start2.id, wp_end2.id)
    seg2 = Segment(
        id=seg2_id, game_id="g", level_number=2,
        start_type="entrance", start_ordinal=0,
        end_type="goal", end_ordinal=0,
        description="L2", ordinal=2,
        start_waypoint_id=wp_start2.id, end_waypoint_id=wp_end2.id,
    )
    practice_db.upsert_segment(seg2)
    # No save state for wp_start2 => state_path will be NULL

    # Seed attempts on BOTH segments so they each have estimates.
    sched = Scheduler(practice_db, "g")
    sched.process_attempt(seg_id, time_ms=5000, completed=True, deaths=0)
    sched.process_attempt(seg2_id, time_ms=8000, completed=True, deaths=0)

    tcp = AsyncMock()
    tcp.is_connected = True
    ps = PracticeSession(tcp=tcp, db=practice_db, game_id="g")
    ps.start()

    # Only seg_id had a real state_path; seg2 contributes nothing.
    # The sum should reflect only seg_id's expected_ms (~5000).
    assert ps.initial_expected_total_ms is not None
    assert ps.initial_expected_total_ms < 6000


def test_snapshot_all_missing_returns_none(practice_db):
    """When no segment has estimates at session start, both snapshots are None."""
    tcp = AsyncMock()
    tcp.is_connected = True
    # No process_attempt call -> no model state -> no expected_ms
    ps = PracticeSession(tcp=tcp, db=practice_db, game_id="g")
    ps.start()

    assert ps.initial_expected_total_ms is None
    assert ps.initial_expected_clean_ms is None


@pytest.mark.asyncio
async def test_practice_session_passes_death_penalty_ms(practice_db):
    """PracticeSession should forward death_penalty_ms to PracticeLoadCmd."""
    seg_id = practice_db._test_seg_id
    mock_tcp = AsyncMock()
    mock_tcp.is_connected = True
    mock_tcp.send_command = AsyncMock()

    session = PracticeSession(tcp=mock_tcp, db=practice_db, game_id="g", death_penalty_ms=2500)
    session.is_running = True

    async def deliver():
        await asyncio.sleep(0.05)
        session.receive_result(AttemptResultEvent(
            segment_id=seg_id,
            completed=True,
            time_ms=4500,
        ))

    asyncio.create_task(deliver())
    await session.run_one()

    mock_tcp.send_command.assert_called_once()
    cmd = mock_tcp.send_command.call_args[0][0]
    assert isinstance(cmd, PracticeLoadCmd)
    assert cmd.death_penalty_ms == 2500


def test_current_expected_times_reflects_model_updates(practice_db):
    """After process_attempt runs, current_expected_times() returns the new sum."""
    seg_id = practice_db._test_seg_id
    sched = Scheduler(practice_db, "g")
    sched.process_attempt(seg_id, time_ms=5000, completed=True, deaths=0)

    tcp = AsyncMock()
    tcp.is_connected = True
    ps = PracticeSession(tcp=tcp, db=practice_db, game_id="g")
    ps.start()
    initial_total = ps.initial_expected_total_ms

    # Simulate a faster attempt pulling the estimate down.
    ps.scheduler.process_attempt(seg_id, time_ms=3000, completed=True, deaths=0)

    cur_total, cur_clean = ps.current_expected_times()
    assert cur_total is not None
    assert cur_total < initial_total
