"""Tests for the async practice loop."""
import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock
from pathlib import Path

from spinlab.db import Database
from spinlab.models import Segment, SegmentVariant
from spinlab.practice import PracticeSession

SEG_ID = "g:1:entrance.0:goal.0"


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    d.upsert_game("g", "Game", "any%")
    state_file = tmp_path / "test.mss"
    state_file.write_bytes(b"fake state")
    seg = Segment(
        id=SEG_ID,
        game_id="g",
        level_number=1,
        start_type="entrance",
        start_ordinal=0,
        end_type="goal",
        end_ordinal=0,
        description="L1",
        ordinal=1,
    )
    d.upsert_segment(seg)
    variant = SegmentVariant(
        segment_id=SEG_ID,
        variant_type="cold",
        state_path=str(state_file),
        is_default=True,
    )
    d.add_variant(variant)
    return d


@pytest.mark.asyncio
async def test_practice_session_picks_and_sends(db):
    """Practice session should pick a segment and send practice_load."""
    mock_tcp = AsyncMock()
    mock_tcp.is_connected = True
    mock_tcp.send = AsyncMock()

    # Simulate receiving an attempt_result after send
    result_event = {
        "event": "attempt_result",
        "segment_id": SEG_ID,
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
    attempts = db.get_segment_attempts(SEG_ID)
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

        ps.scheduler.pick_next = MagicMock(return_value=mock_segment)
        ps.scheduler.peek_next_n = MagicMock(return_value=[])
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
