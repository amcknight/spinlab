"""Tests for the async practice loop."""
import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock
from pathlib import Path

from spinlab.db import Database
from spinlab.models import Split
from spinlab.practice import PracticeSession


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    d.upsert_game("g", "Game", "any%")
    state_file = tmp_path / "test.mss"
    state_file.write_bytes(b"fake state")
    s = Split(id="g:1:0:normal", game_id="g", level_number=1, room_id=0,
              goal="normal", description="L1", state_path=str(state_file),
              reference_time_ms=5000, ordinal=1)
    d.upsert_split(s)
    return d


@pytest.mark.asyncio
async def test_practice_session_picks_and_sends(db):
    """Practice session should pick a split and send practice_load."""
    mock_tcp = AsyncMock()
    mock_tcp.is_connected = True
    mock_tcp.send = AsyncMock()

    # Simulate receiving an attempt_result after send
    result_event = {
        "event": "attempt_result",
        "split_id": "g:1:0:normal",
        "completed": True,
        "time_ms": 4500,
        "goal": "normal",
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
    attempts = db.get_split_attempts("g:1:0:normal")
    assert len(attempts) == 1
    assert attempts[0]["completed"] == 1


@pytest.mark.asyncio
async def test_practice_session_state(db):
    session = PracticeSession(tcp=AsyncMock(), db=db, game_id="g")
    assert session.is_running is False
    assert session.current_split_id is None
    assert session.splits_attempted == 0


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
        db.get_all_splits_with_model = MagicMock(return_value=[])
        db.load_model_state = MagicMock(return_value=None)
        db.save_model_state = MagicMock()

        ps = PracticeSession(tcp=tcp, db=db, game_id="test")
        ps.is_running = True

        # Create a real state file so os.path.exists passes
        state_file = tmp_path / "test.mss"
        state_file.write_bytes(b"fake state")

        # Simulate scheduler returning a split
        mock_split = MagicMock()
        mock_split.split_id = "s1"
        mock_split.state_path = str(state_file)
        mock_split.goal = "normal"
        mock_split.description = "Test"
        mock_split.reference_time_ms = 5000
        mock_split.estimator_state = None
        mock_split.end_on_goal = True

        ps.scheduler.pick_next = MagicMock(return_value=mock_split)
        ps.scheduler.peek_next_n = MagicMock(return_value=[])
        ps.scheduler.process_attempt = MagicMock()

        # Schedule receive_result after a short delay
        async def deliver_result():
            await asyncio.sleep(0.1)
            ps.receive_result({
                "event": "attempt_result",
                "split_id": "s1",
                "completed": True,
                "time_ms": 4500,
                "goal": "normal",
            })

        asyncio.create_task(deliver_result())
        result = await ps.run_one()

        assert result is True
        assert ps.splits_completed == 1
