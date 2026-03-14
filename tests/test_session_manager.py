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
