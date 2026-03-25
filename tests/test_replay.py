# tests/test_replay.py
"""Tests for SessionManager replay orchestration."""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from spinlab.session_manager import SessionManager


def make_mock_tcp():
    tcp = MagicMock()
    tcp.is_connected = True
    tcp.send = AsyncMock()
    tcp.recv_event = AsyncMock(return_value=None)
    return tcp


def make_mock_db():
    db = MagicMock()
    db.upsert_game = MagicMock()
    db.create_session = MagicMock()
    db.end_session = MagicMock()
    db.create_capture_run = MagicMock()
    db.set_active_capture_run = MagicMock()
    db.get_recent_attempts = MagicMock(return_value=[])
    db.get_all_segments_with_model = MagicMock(return_value=[])
    db.load_model_state = MagicMock(return_value=None)
    db.load_allocator_config = MagicMock(return_value=None)
    db.upsert_segment = MagicMock()
    db.add_variant = MagicMock()
    db.get_active_segments = MagicMock(return_value=[])
    return db


class TestStartReplay:
    @pytest.mark.asyncio
    async def test_sends_replay_command(self, tmp_path):
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=tmp_path, default_category="any%", data_dir=tmp_path)
        sm.game_id = "abcdef0123456789"
        sm.game_name = "Test Game"

        result = await sm.start_replay("/data/test.spinrec", speed=0)
        assert result["status"] == "started"
        assert sm.mode == "replay"

        msg = json.loads(tcp.send.call_args[0][0])
        assert msg["event"] == "replay"
        assert msg["path"] == "/data/test.spinrec"
        assert msg["speed"] == 0

    @pytest.mark.asyncio
    async def test_rejects_during_practice(self, tmp_path):
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=tmp_path, default_category="any%", data_dir=tmp_path)
        sm.game_id = "abcdef0123456789"
        sm.mode = "practice"

        result = await sm.start_replay("/data/test.spinrec")
        assert result["status"] == "practice_active"

    @pytest.mark.asyncio
    async def test_rejects_during_reference(self, tmp_path):
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=tmp_path, default_category="any%", data_dir=tmp_path)
        sm.game_id = "abcdef0123456789"
        sm.mode = "reference"

        result = await sm.start_replay("/data/test.spinrec")
        assert result["status"] == "reference_active"


class TestStopReplay:
    @pytest.mark.asyncio
    async def test_sends_stop_command(self, tmp_path):
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=tmp_path, default_category="any%", data_dir=tmp_path)
        sm.game_id = "abcdef0123456789"
        sm.game_name = "Test Game"
        sm.mode = "replay"

        result = await sm.stop_replay()
        assert result["status"] == "stopped"
        assert sm.mode == "idle"

        msg = json.loads(tcp.send.call_args[0][0])
        assert msg["event"] == "replay_stop"


class TestReplayEvents:
    @pytest.mark.asyncio
    async def test_replay_finished_returns_to_idle(self, tmp_path):
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=tmp_path, default_category="any%", data_dir=tmp_path)
        sm.game_id = "abcdef0123456789"
        sm.game_name = "Test Game"
        sm.mode = "replay"

        await sm.route_event({"event": "replay_finished", "path": "/data/test.spinrec", "frames_played": 5000})
        assert sm.mode == "idle"

    @pytest.mark.asyncio
    async def test_replay_error_returns_to_idle(self, tmp_path):
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=tmp_path, default_category="any%", data_dir=tmp_path)
        sm.game_id = "abcdef0123456789"
        sm.mode = "replay"

        await sm.route_event({"event": "replay_error", "message": "game_id mismatch"})
        assert sm.mode == "idle"

    @pytest.mark.asyncio
    async def test_replay_events_still_capture_segments(self, tmp_path):
        """Events with source=replay still flow through reference capture pipeline."""
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=tmp_path, default_category="any%", data_dir=tmp_path)
        sm.game_id = "abcdef0123456789"
        sm.game_name = "Test Game"
        sm.mode = "replay"
        sm.ref_capture_run_id = "replay_test123"

        # Simulate a level entrance during replay
        await sm.route_event({
            "event": "level_entrance",
            "level_num": 0x105,
            "level": 0x105,
            "room": 0,
            "frame": 100,
            "ts_ms": 1000,
            "session": "passive",
            "state_path": "/data/test.mss",
            "source": "replay",
        })
        # Reference capture works during replay — segments are created
        assert sm.ref_pending_start is not None
