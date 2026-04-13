"""Tests for draft reference lifecycle in SessionManager."""
import pytest

from spinlab.models import Mode, Status
from spinlab.session_manager import SessionManager


def make_sm(mock_db, mock_tcp, tmp_path):
    sm = SessionManager(db=mock_db, tcp=mock_tcp, rom_dir=tmp_path, default_category="any%", data_dir=tmp_path)
    sm.game_id = "abcdef0123456789"
    sm.game_name = "Test Game"
    return sm


class TestStopReferenceCreatesDraft:
    async def test_stop_reference_enters_draft_state(self, mock_db, mock_tcp, tmp_path):
        sm = make_sm(mock_db, mock_tcp, tmp_path)
        await sm.start_reference()
        run_id = sm.ref_capture.capture_run_id
        sm.ref_capture.segments_count = 5

        await sm.stop_reference()
        assert sm.mode == Mode.IDLE
        assert sm.draft.run_id == run_id
        assert sm.draft.segments_count == 5


class TestReplayCreatesDraft:
    async def test_replay_finished_enters_draft_state(self, mock_db, mock_tcp, tmp_path):
        sm = make_sm(mock_db, mock_tcp, tmp_path)
        sm.mode = Mode.REPLAY
        sm.ref_capture.capture_run_id = "replay_abc"
        sm.ref_capture.segments_count = 8

        await sm.route_event({"event": "replay_finished", "frames_played": 5000})
        assert sm.mode == Mode.IDLE
        assert sm.draft.run_id == "replay_abc"
        assert sm.draft.segments_count == 8

    async def test_replay_error_with_segments_enters_draft(self, mock_db, mock_tcp, tmp_path):
        sm = make_sm(mock_db, mock_tcp, tmp_path)
        sm.mode = Mode.REPLAY
        sm.ref_capture.capture_run_id = "replay_abc"
        sm.ref_capture.segments_count = 3

        await sm.route_event({"event": "replay_error", "message": "fail"})
        assert sm.draft.run_id == "replay_abc"
        assert sm.draft.segments_count == 3

    async def test_replay_error_no_segments_auto_discards(self, mock_db, mock_tcp, tmp_path):
        sm = make_sm(mock_db, mock_tcp, tmp_path)
        sm.mode = Mode.REPLAY
        sm.ref_capture.capture_run_id = "replay_abc"
        sm.ref_capture.segments_count = 0

        await sm.route_event({"event": "replay_error", "message": "fail"})
        assert sm.draft.run_id is None
        mock_db.hard_delete_capture_run.assert_called_once_with("replay_abc")


class TestDraftGuards:
    async def test_start_reference_blocked_by_draft(self, mock_db, mock_tcp, tmp_path):
        sm = make_sm(mock_db, mock_tcp, tmp_path)
        sm.draft.run_id = "draft_pending"
        result = await sm.start_reference()
        assert result.status == Status.DRAFT_PENDING

    async def test_start_replay_blocked_by_draft(self, mock_db, mock_tcp, tmp_path):
        sm = make_sm(mock_db, mock_tcp, tmp_path)
        sm.draft.run_id = "draft_pending"
        result = await sm.start_replay("/data/test.spinrec")
        assert result.status == Status.DRAFT_PENDING

    async def test_start_practice_blocked_by_draft(self, mock_db, mock_tcp, tmp_path):
        sm = make_sm(mock_db, mock_tcp, tmp_path)
        sm.draft.run_id = "draft_pending"
        result = await sm.start_practice()
        assert result.status == Status.DRAFT_PENDING


class TestSaveAndDiscard:
    async def test_save_draft(self, mock_db, mock_tcp, tmp_path):
        sm = make_sm(mock_db, mock_tcp, tmp_path)
        sm.draft.run_id = "live_abc"
        sm.draft.segments_count = 5

        result = await sm.save_draft("My Run")
        assert result.status == Status.OK
        assert sm.draft.run_id is None
        assert sm.draft.segments_count == 0
        mock_db.promote_draft.assert_called_once_with("live_abc", "My Run")
        mock_db.set_active_capture_run.assert_called_once_with("live_abc")

    async def test_discard_draft(self, mock_db, mock_tcp, tmp_path):
        sm = make_sm(mock_db, mock_tcp, tmp_path)
        sm.draft.run_id = "live_abc"
        sm.draft.segments_count = 5

        result = await sm.discard_draft()
        assert result.status == Status.OK
        assert sm.draft.run_id is None
        assert sm.draft.segments_count == 0
        mock_db.hard_delete_capture_run.assert_called_once_with("live_abc")

    async def test_save_no_draft_returns_error(self, mock_db, mock_tcp, tmp_path):
        sm = make_sm(mock_db, mock_tcp, tmp_path)
        result = await sm.save_draft("Name")
        assert result.status == Status.NO_DRAFT


class TestStopReplayDraft:
    async def test_stop_replay_with_segments_enters_draft(self, mock_db, mock_tcp, tmp_path):
        sm = make_sm(mock_db, mock_tcp, tmp_path)
        sm.mode = Mode.REPLAY
        sm.ref_capture.capture_run_id = "replay_abc"
        sm.ref_capture.segments_count = 5

        result = await sm.stop_replay()
        assert result.status == Status.STOPPED
        assert sm.draft.run_id == "replay_abc"
        assert sm.draft.segments_count == 5

    async def test_stop_replay_no_segments_auto_discards(self, mock_db, mock_tcp, tmp_path):
        sm = make_sm(mock_db, mock_tcp, tmp_path)
        sm.mode = Mode.REPLAY
        sm.ref_capture.capture_run_id = "replay_abc"
        sm.ref_capture.segments_count = 0

        result = await sm.stop_replay()
        assert result.status == Status.STOPPED
        assert sm.draft.run_id is None
        mock_db.hard_delete_capture_run.assert_called_once_with("replay_abc")


class TestOnDisconnectDraft:
    def test_disconnect_with_segments_enters_draft(self, mock_db, mock_tcp, tmp_path):
        sm = make_sm(mock_db, mock_tcp, tmp_path)
        sm.mode = Mode.REFERENCE
        sm.ref_capture.capture_run_id = "live_abc"
        sm.ref_capture.segments_count = 3

        sm.on_disconnect()
        assert sm.draft.run_id == "live_abc"
        assert sm.draft.segments_count == 3

    def test_disconnect_no_segments_auto_discards(self, mock_db, mock_tcp, tmp_path):
        sm = make_sm(mock_db, mock_tcp, tmp_path)
        sm.mode = Mode.REFERENCE
        sm.ref_capture.capture_run_id = "live_abc"
        sm.ref_capture.segments_count = 0

        sm.on_disconnect()
        assert sm.draft.run_id is None
        mock_db.hard_delete_capture_run.assert_called_once_with("live_abc")

    def test_disconnect_no_ref_state_is_noop(self, mock_db, mock_tcp, tmp_path):
        sm = make_sm(mock_db, mock_tcp, tmp_path)
        sm.mode = Mode.IDLE

        sm.on_disconnect()
        assert sm.draft.run_id is None
        mock_db.hard_delete_capture_run.assert_not_called()


class TestGetStateDraft:
    def test_get_state_includes_draft(self, mock_db, mock_tcp, tmp_path):
        sm = make_sm(mock_db, mock_tcp, tmp_path)
        sm.draft.run_id = "live_abc"
        sm.draft.segments_count = 12
        state = sm.get_state()
        assert state["draft"] == {"run_id": "live_abc", "segments_captured": 12}
