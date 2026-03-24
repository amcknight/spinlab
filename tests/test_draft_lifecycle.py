"""Tests for draft reference lifecycle in SessionManager."""
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
    db.promote_draft = MagicMock()
    db.hard_delete_capture_run = MagicMock()
    return db


def make_sm(tmp_path):
    db = make_mock_db()
    tcp = make_mock_tcp()
    sm = SessionManager(db=db, tcp=tcp, rom_dir=tmp_path, default_category="any%", data_dir=tmp_path)
    sm.game_id = "abcdef0123456789"
    sm.game_name = "Test Game"
    return sm, db, tcp


class TestStopReferenceCreatesDraft:
    @pytest.mark.asyncio
    async def test_stop_reference_enters_draft_state(self, tmp_path):
        sm, db, tcp = make_sm(tmp_path)
        await sm.start_reference()
        run_id = sm.ref_capture_run_id
        sm.ref_segments_count = 5

        await sm.stop_reference()
        assert sm.mode == "idle"
        assert sm.draft_run_id == run_id
        assert sm.draft_segments_count == 5

    @pytest.mark.asyncio
    async def test_start_reference_creates_draft_run(self, tmp_path):
        sm, db, tcp = make_sm(tmp_path)
        await sm.start_reference()
        db.create_capture_run.assert_called_once()
        call_kwargs = db.create_capture_run.call_args
        # draft=True should be passed
        assert call_kwargs[1].get("draft") is True or call_kwargs[0][3] is True  # positional or keyword

    @pytest.mark.asyncio
    async def test_start_reference_does_not_set_active(self, tmp_path):
        sm, db, tcp = make_sm(tmp_path)
        await sm.start_reference()
        db.set_active_capture_run.assert_not_called()


class TestReplayCreatesDraft:
    @pytest.mark.asyncio
    async def test_replay_finished_enters_draft_state(self, tmp_path):
        sm, db, tcp = make_sm(tmp_path)
        # Simulate active replay
        sm.mode = "replay"
        sm.ref_capture_run_id = "replay_abc"
        sm.ref_segments_count = 8

        await sm.route_event({"event": "replay_finished", "frames_played": 5000})
        assert sm.mode == "idle"
        assert sm.draft_run_id == "replay_abc"
        assert sm.draft_segments_count == 8

    @pytest.mark.asyncio
    async def test_replay_error_with_segments_enters_draft(self, tmp_path):
        sm, db, tcp = make_sm(tmp_path)
        sm.mode = "replay"
        sm.ref_capture_run_id = "replay_abc"
        sm.ref_segments_count = 3

        await sm.route_event({"event": "replay_error", "message": "fail"})
        assert sm.draft_run_id == "replay_abc"
        assert sm.draft_segments_count == 3

    @pytest.mark.asyncio
    async def test_replay_error_no_segments_auto_discards(self, tmp_path):
        sm, db, tcp = make_sm(tmp_path)
        sm.mode = "replay"
        sm.ref_capture_run_id = "replay_abc"
        sm.ref_segments_count = 0

        await sm.route_event({"event": "replay_error", "message": "fail"})
        assert sm.draft_run_id is None
        db.hard_delete_capture_run.assert_called_once_with("replay_abc")


class TestDraftGuards:
    @pytest.mark.asyncio
    async def test_start_reference_blocked_by_draft(self, tmp_path):
        sm, db, tcp = make_sm(tmp_path)
        sm.draft_run_id = "draft_pending"
        result = await sm.start_reference()
        assert result["status"] == "draft_pending"

    @pytest.mark.asyncio
    async def test_start_replay_blocked_by_draft(self, tmp_path):
        sm, db, tcp = make_sm(tmp_path)
        sm.draft_run_id = "draft_pending"
        result = await sm.start_replay("/data/test.spinrec")
        assert result["status"] == "draft_pending"

    @pytest.mark.asyncio
    async def test_start_practice_blocked_by_draft(self, tmp_path):
        sm, db, tcp = make_sm(tmp_path)
        sm.draft_run_id = "draft_pending"
        result = await sm.start_practice()
        assert result["status"] == "draft_pending"


class TestSaveAndDiscard:
    @pytest.mark.asyncio
    async def test_save_draft(self, tmp_path):
        sm, db, tcp = make_sm(tmp_path)
        sm.draft_run_id = "live_abc"
        sm.draft_segments_count = 5

        result = await sm.save_draft("My Run")
        assert result["status"] == "ok"
        assert sm.draft_run_id is None
        assert sm.draft_segments_count == 0
        db.promote_draft.assert_called_once_with("live_abc", "My Run")
        db.set_active_capture_run.assert_called_once_with("live_abc")

    @pytest.mark.asyncio
    async def test_discard_draft(self, tmp_path):
        sm, db, tcp = make_sm(tmp_path)
        sm.draft_run_id = "live_abc"
        sm.draft_segments_count = 5

        result = await sm.discard_draft()
        assert result["status"] == "ok"
        assert sm.draft_run_id is None
        assert sm.draft_segments_count == 0
        db.hard_delete_capture_run.assert_called_once_with("live_abc")

    @pytest.mark.asyncio
    async def test_save_no_draft_returns_error(self, tmp_path):
        sm, db, tcp = make_sm(tmp_path)
        result = await sm.save_draft("Name")
        assert result["status"] == "no_draft"


class TestGetStateDraft:
    @pytest.mark.asyncio
    async def test_get_state_includes_draft(self, tmp_path):
        sm, db, tcp = make_sm(tmp_path)
        sm.draft_run_id = "live_abc"
        sm.draft_segments_count = 12
        state = sm.get_state()
        assert state["draft"] == {"run_id": "live_abc", "segments_captured": 12}

    @pytest.mark.asyncio
    async def test_get_state_no_draft(self, tmp_path):
        sm, db, tcp = make_sm(tmp_path)
        state = sm.get_state()
        assert state.get("draft") is None
