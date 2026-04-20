"""Tests for StateBuilder — covers branches not already exercised by
test_dashboard_integration.py (which covers the practice branch).
"""
from unittest.mock import MagicMock

import pytest

from spinlab.models import Mode
from spinlab.state_builder import StateBuilder


class TestIdleBaseCase:
    def test_no_game_returns_bare_state(self, mock_db):
        sb = StateBuilder(mock_db)
        sm = MagicMock()
        sm.mode = Mode.IDLE
        sm.tcp.is_connected = True
        sm.game_id = None
        sm.game_name = None
        sm.capture.sections_captured = 0

        state = sb.build(sm)

        assert state["mode"] == "idle"
        assert state["game_id"] is None
        assert state["game_name"] is None
        assert state["current_segment"] is None
        assert state["recent"] == []
        assert state["session"] is None
        assert state["allocator_weights"] is None
        assert state["estimator"] is None


class TestSpeedRunBranch:
    def test_speed_run_populates_current_level(self, mock_db):
        sb = StateBuilder(mock_db)
        sm = MagicMock()
        sm.mode = Mode.SPEED_RUN
        sm.tcp.is_connected = True
        sm.game_id = "g1"
        sm.game_name = "Test"
        sm.capture.sections_captured = 0
        sm.capture.get_draft_state.return_value = None

        fake_sched = MagicMock()
        fake_sched.allocator.entries = []
        fake_sched.estimator.name = "kalman"
        sm.get_scheduler.return_value = fake_sched

        fake_level = MagicMock()
        fake_level.level_number = 5
        fake_level.description = "Level 5"
        fake_level.entrance_state_path = "/tmp/l5.mss"
        fake_level.segments = [{"id": "seg-l5"}]

        sr = MagicMock()
        sr.session_id = "sr-abc"
        sr.started_at = "2026-04-10T12:00:00"
        sr.segments_recorded = 3
        sr.levels_completed = 2
        sr.current_level_index = 0
        sr.levels = [fake_level]
        sr.game_id = "g1"
        sm.speed_run_session = sr

        state = sb.build(sm)

        assert state["mode"] == "speed_run"
        assert state["session"]["id"] == "sr-abc"
        assert state["session"]["segments_attempted"] == 3
        assert state["session"]["segments_completed"] == 2
        assert state["current_segment"]["level_number"] == 5
        assert state["current_segment"]["description"] == "Level 5"
        assert state["current_segment"]["state_path"] == "/tmp/l5.mss"
        assert state["current_segment"]["id"] == "seg-l5"


class TestColdFillBranch:
    def test_cold_fill_includes_state(self, mock_db):
        sb = StateBuilder(mock_db)
        sm = MagicMock()
        sm.mode = Mode.COLD_FILL
        sm.tcp.is_connected = True
        sm.game_id = "g1"
        sm.game_name = "Test"
        sm.capture.sections_captured = 0
        sm.capture.get_draft_state.return_value = None

        fake_sched = MagicMock()
        fake_sched.allocator.entries = []
        fake_sched.estimator.name = "kalman"
        sm.get_scheduler.return_value = fake_sched

        sm.cold_fill.get_state.return_value = {
            "current_segment_id": "seg1",
            "remaining": 3,
            "total": 5,
        }

        state = sb.build(sm)
        assert state["mode"] == "cold_fill"
        assert state["cold_fill"]["remaining"] == 3
        assert state["cold_fill"]["total"] == 5

    def test_cold_fill_none_state_omitted(self, mock_db):
        """When cold_fill.get_state() returns None, no cold_fill key is added."""
        sb = StateBuilder(mock_db)
        sm = MagicMock()
        sm.mode = Mode.COLD_FILL
        sm.tcp.is_connected = True
        sm.game_id = "g1"
        sm.game_name = "Test"
        sm.capture.sections_captured = 0
        sm.capture.get_draft_state.return_value = None

        fake_sched = MagicMock()
        fake_sched.allocator.entries = []
        fake_sched.estimator.name = "kalman"
        sm.get_scheduler.return_value = fake_sched

        sm.cold_fill.get_state.return_value = None

        state = sb.build(sm)
        assert "cold_fill" not in state


class TestDraftBranch:
    def test_draft_state_included_when_active(self, mock_db):
        sb = StateBuilder(mock_db)
        sm = MagicMock()
        sm.mode = Mode.IDLE
        sm.tcp.is_connected = True
        sm.game_id = "g1"
        sm.game_name = "Test"
        sm.capture.sections_captured = 7

        fake_sched = MagicMock()
        fake_sched.allocator.entries = []
        fake_sched.estimator.name = "kalman"
        sm.get_scheduler.return_value = fake_sched

        sm.capture.get_draft_state.return_value = {
            "run_id": "run-xyz", "segment_count": 7,
        }

        state = sb.build(sm)
        assert state["draft"] == {"run_id": "run-xyz", "segment_count": 7}
