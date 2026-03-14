# tests/test_session_manager.py
"""Tests for SessionManager state machine."""
import asyncio
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
    db.get_all_splits_with_model = MagicMock(return_value=[])
    db.load_model_state = MagicMock(return_value=None)
    db.load_allocator_config = MagicMock(return_value=None)
    db.upsert_split = MagicMock()
    return db


class TestSessionManagerInit:
    def test_initial_state(self):
        sm = SessionManager(
            db=make_mock_db(),
            tcp=make_mock_tcp(),
            rom_dir=None,
            default_category="any%",
        )
        assert sm.mode == "idle"
        assert sm.game_id is None
        assert sm.game_name is None
        assert sm.scheduler is None
        assert sm.practice_session is None
        assert sm.practice_task is None

    def test_get_state_no_game(self):
        sm = SessionManager(
            db=make_mock_db(),
            tcp=make_mock_tcp(),
            rom_dir=None,
            default_category="any%",
        )
        state = sm.get_state()
        assert state["mode"] == "idle"
        assert state["game_id"] is None
        assert state["tcp_connected"] is True


class TestRouteEvent:
    @pytest.mark.asyncio
    async def test_rom_info_discovers_game(self, tmp_path):
        """rom_info event triggers game discovery via checksum."""
        rom_file = tmp_path / "test_hack.sfc"
        rom_file.write_bytes(b"\x00" * 1024)  # dummy ROM

        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=tmp_path, default_category="any%")

        await sm.route_event({"event": "rom_info", "filename": "test_hack.sfc"})

        assert sm.game_id is not None
        assert sm.game_name is not None
        db.upsert_game.assert_called_once()
        tcp.send.assert_called_once()  # game_context sent back

    @pytest.mark.asyncio
    async def test_rom_info_no_rom_dir(self):
        """rom_info with no rom_dir uses fallback ID."""
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=None, default_category="any%")

        await sm.route_event({"event": "rom_info", "filename": "test.sfc"})
        # No rom_dir → no game discovery
        assert sm.game_id is None

    @pytest.mark.asyncio
    async def test_game_context_switches_game(self):
        """game_context event triggers switch_game."""
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=None, default_category="any%")

        await sm.route_event({
            "event": "game_context",
            "game_id": "abc123",
            "game_name": "Test Game",
        })

        assert sm.game_id == "abc123"
        assert sm.game_name == "Test Game"

    @pytest.mark.asyncio
    async def test_level_entrance_in_reference_mode(self):
        """level_entrance buffered during reference mode."""
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=None, default_category="any%")
        sm.game_id = "game1"
        sm.mode = "reference"
        sm.ref_capture_run_id = "run1"

        await sm.route_event({
            "event": "level_entrance",
            "level": 105,
            "room": 0,
            "state_path": "/path/to/state.mss",
        })

        assert (105, 0) in sm.ref_pending

    @pytest.mark.asyncio
    async def test_level_exit_pairs_with_entrance(self):
        """level_exit in reference mode pairs with pending entrance to create split."""
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=None, default_category="any%")
        sm.game_id = "game1"
        sm.mode = "reference"
        sm.ref_capture_run_id = "run1"

        # Buffer entrance
        await sm.route_event({
            "event": "level_entrance",
            "level": 105,
            "room": 0,
            "state_path": "/path/to/state.mss",
        })

        # Exit with goal
        await sm.route_event({
            "event": "level_exit",
            "level": 105,
            "room": 0,
            "goal": "normal",
            "elapsed_ms": 5000,
        })

        assert sm.ref_splits_count == 1
        db.upsert_split.assert_called_once()

    @pytest.mark.asyncio
    async def test_level_exit_abort_discards(self):
        """level_exit with goal=abort discards pending entrance."""
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=None, default_category="any%")
        sm.game_id = "game1"
        sm.mode = "reference"

        await sm.route_event({
            "event": "level_entrance",
            "level": 105,
            "room": 0,
        })
        await sm.route_event({
            "event": "level_exit",
            "level": 105,
            "room": 0,
            "goal": "abort",
        })

        assert sm.ref_splits_count == 0
        db.upsert_split.assert_not_called()

    @pytest.mark.asyncio
    async def test_events_ignored_outside_reference(self):
        """level_entrance/exit ignored when not in reference mode."""
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=None, default_category="any%")
        sm.game_id = "game1"
        sm.mode = "idle"

        await sm.route_event({"event": "level_entrance", "level": 1, "room": 0})
        await sm.route_event({"event": "level_exit", "level": 1, "room": 0, "goal": "normal"})

        assert len(sm.ref_pending) == 0
        assert sm.ref_splits_count == 0
