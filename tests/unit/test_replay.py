# tests/test_replay.py
"""Tests for SessionManager replay orchestration."""
import pytest

from spinlab.errors import PracticeActiveError, ReferenceActiveError
from spinlab.models import Mode, Status
from spinlab.protocol import (
    LevelEntranceEvent,
    ReplayCmd,
    ReplayErrorEvent,
    ReplayFinishedEvent,
)
from spinlab.session_manager import SessionManager


class TestStartReplay:
    async def test_sends_replay_command(self, mock_db, mock_tcp, tmp_path):
        sm = SessionManager(db=mock_db, tcp=mock_tcp, rom_dir=tmp_path, default_category="any%", data_dir=tmp_path)
        sm.game_id = "abcdef0123456789"
        sm.game_name = "Test Game"

        result = await sm.start_replay("/data/test.spinrec", speed=0)
        assert result.status == Status.STARTED
        assert sm.mode == Mode.REPLAY

        cmd = mock_tcp.send_command.call_args[0][0]
        assert isinstance(cmd, ReplayCmd)
        assert cmd.path == "/data/test.spinrec"
        assert cmd.speed == 0

    async def test_rejects_during_practice(self, mock_db, mock_tcp, tmp_path):
        sm = SessionManager(db=mock_db, tcp=mock_tcp, rom_dir=tmp_path, default_category="any%", data_dir=tmp_path)
        sm.game_id = "abcdef0123456789"
        sm.mode = Mode.PRACTICE

        with pytest.raises(PracticeActiveError):
            await sm.start_replay("/data/test.spinrec")

    async def test_rejects_during_reference(self, mock_db, mock_tcp, tmp_path):
        sm = SessionManager(db=mock_db, tcp=mock_tcp, rom_dir=tmp_path, default_category="any%", data_dir=tmp_path)
        sm.game_id = "abcdef0123456789"
        sm.mode = Mode.REFERENCE

        with pytest.raises(ReferenceActiveError):
            await sm.start_replay("/data/test.spinrec")


class TestReplayEvents:
    async def test_replay_finished_returns_to_idle(self, mock_db, mock_tcp, tmp_path):
        sm = SessionManager(db=mock_db, tcp=mock_tcp, rom_dir=tmp_path, default_category="any%", data_dir=tmp_path)
        sm.game_id = "abcdef0123456789"
        sm.game_name = "Test Game"
        sm.mode = Mode.REPLAY

        await sm.route_event(ReplayFinishedEvent())
        assert sm.mode == Mode.IDLE

    async def test_replay_error_returns_to_idle(self, mock_db, mock_tcp, tmp_path):
        sm = SessionManager(db=mock_db, tcp=mock_tcp, rom_dir=tmp_path, default_category="any%", data_dir=tmp_path)
        sm.game_id = "abcdef0123456789"
        sm.mode = Mode.REPLAY

        await sm.route_event(ReplayErrorEvent(message="game_id mismatch"))
        assert sm.mode == Mode.IDLE

    async def test_replay_events_still_capture_segments(self, mock_db, mock_tcp, tmp_path):
        """Events with source=replay still flow through reference capture pipeline."""
        sm = SessionManager(db=mock_db, tcp=mock_tcp, rom_dir=tmp_path, default_category="any%", data_dir=tmp_path)
        sm.game_id = "abcdef0123456789"
        sm.game_name = "Test Game"
        sm.mode = Mode.REPLAY
        sm.capture.recorder.capture_run_id = "replay_test123"

        await sm.route_event(LevelEntranceEvent(
            level=0x105, room=0, frame=100, state_path="/data/test.mss",
        ))
        assert sm.capture.recorder.pending_start is not None
