# tests/test_replay.py
"""Tests for SessionManager replay orchestration."""
import json

import pytest

from spinlab.models import Mode, Status
from spinlab.session_manager import SessionManager


class TestStartReplay:
    async def test_sends_replay_command(self, mock_db, mock_tcp, tmp_path):
        sm = SessionManager(db=mock_db, tcp=mock_tcp, rom_dir=tmp_path, default_category="any%", data_dir=tmp_path)
        sm.game_id = "abcdef0123456789"
        sm.game_name = "Test Game"

        result = await sm.start_replay("/data/test.spinrec", speed=0)
        assert result.status == Status.STARTED
        assert sm.mode == Mode.REPLAY

        msg = json.loads(mock_tcp.send.call_args[0][0])
        assert msg["event"] == "replay"
        assert msg["path"] == "/data/test.spinrec"
        assert msg["speed"] == 0

    async def test_rejects_during_practice(self, mock_db, mock_tcp, tmp_path):
        sm = SessionManager(db=mock_db, tcp=mock_tcp, rom_dir=tmp_path, default_category="any%", data_dir=tmp_path)
        sm.game_id = "abcdef0123456789"
        sm.mode = Mode.PRACTICE

        result = await sm.start_replay("/data/test.spinrec")
        assert result.status == Status.PRACTICE_ACTIVE

    async def test_rejects_during_reference(self, mock_db, mock_tcp, tmp_path):
        sm = SessionManager(db=mock_db, tcp=mock_tcp, rom_dir=tmp_path, default_category="any%", data_dir=tmp_path)
        sm.game_id = "abcdef0123456789"
        sm.mode = Mode.REFERENCE

        result = await sm.start_replay("/data/test.spinrec")
        assert result.status == Status.REFERENCE_ACTIVE


class TestReplayEvents:
    async def test_replay_finished_returns_to_idle(self, mock_db, mock_tcp, tmp_path):
        sm = SessionManager(db=mock_db, tcp=mock_tcp, rom_dir=tmp_path, default_category="any%", data_dir=tmp_path)
        sm.game_id = "abcdef0123456789"
        sm.game_name = "Test Game"
        sm.mode = Mode.REPLAY

        await sm.route_event({"event": "replay_finished", "path": "/data/test.spinrec", "frames_played": 5000})
        assert sm.mode == Mode.IDLE

    async def test_replay_error_returns_to_idle(self, mock_db, mock_tcp, tmp_path):
        sm = SessionManager(db=mock_db, tcp=mock_tcp, rom_dir=tmp_path, default_category="any%", data_dir=tmp_path)
        sm.game_id = "abcdef0123456789"
        sm.mode = Mode.REPLAY

        await sm.route_event({"event": "replay_error", "message": "game_id mismatch"})
        assert sm.mode == Mode.IDLE

    async def test_replay_events_still_capture_segments(self, mock_db, mock_tcp, tmp_path):
        """Events with source=replay still flow through reference capture pipeline."""
        sm = SessionManager(db=mock_db, tcp=mock_tcp, rom_dir=tmp_path, default_category="any%", data_dir=tmp_path)
        sm.game_id = "abcdef0123456789"
        sm.game_name = "Test Game"
        sm.mode = Mode.REPLAY
        sm.ref_capture.capture_run_id = "replay_test123"

        await sm.route_event({
            "event": "level_entrance",
            "level_num": 0x105, "level": 0x105, "room": 0,
            "frame": 100, "ts_ms": 1000,
            "session": "passive", "state_path": "/data/test.mss",
            "source": "replay",
        })
        assert sm.ref_capture.pending_start is not None
