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

    async def fake_recv_event(timeout=None):
        return result_event

    mock_tcp.recv_event = fake_recv_event

    session = PracticeSession(tcp=mock_tcp, db=db, game_id="g")
    session.is_running = True
    # Run one iteration
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
