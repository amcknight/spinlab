"""Tests for reference and segment management API endpoints."""
import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
from spinlab.db import Database
from spinlab.models import ActionResult, Segment, Status


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    d.upsert_game("test_game", "Test Game", "any%")
    return d


@pytest.fixture
def client(db, tmp_path):
    from spinlab.dashboard import create_app
    from conftest import make_test_config
    app = create_app(db=db, config=make_test_config())
    app.state.session.game_id = "test_game"
    app.state.session.game_name = "Test Game"
    return TestClient(app)


class TestReferenceEndpoints:
    def test_list_references(self, client, db):
        db.create_capture_run("ref1", "test_game", "Run 1")
        resp = client.get("/api/references")
        assert resp.status_code == 200
        assert len(resp.json()["references"]) == 1

    def test_create_reference(self, client):
        resp = client.post("/api/references", json={"name": "New Run"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "New Run"

    def test_rename_reference(self, client, db):
        db.create_capture_run("ref1", "test_game", "Old")
        resp = client.patch("/api/references/ref1", json={"name": "New"})
        assert resp.status_code == 200
        refs = db.list_capture_runs("test_game")
        assert refs[0]["name"] == "New"

    def test_delete_reference(self, client, db):
        db.create_capture_run("ref1", "test_game", "Run 1")
        s = Segment(id="s1", game_id="test_game", level_number=1,
                    start_type="entrance", start_ordinal=0,
                    end_type="goal", end_ordinal=0,
                    reference_id="ref1")
        db.upsert_segment(s)
        resp = client.delete("/api/references/ref1")
        assert resp.status_code == 200
        assert db.list_capture_runs("test_game") == []

    def test_activate_reference(self, client, db):
        db.create_capture_run("ref1", "test_game", "Run 1")
        db.create_capture_run("ref2", "test_game", "Run 2")
        resp = client.post("/api/references/ref2/activate")
        assert resp.status_code == 200
        refs = db.list_capture_runs("test_game")
        active = [r for r in refs if r["active"]]
        assert active[0]["id"] == "ref2"


class TestSegmentEditEndpoints:
    def test_patch_segment(self, client, db):
        s = Segment(id="s1", game_id="test_game", level_number=1,
                    start_type="entrance", start_ordinal=0,
                    end_type="goal", end_ordinal=0)
        db.upsert_segment(s)
        resp = client.patch("/api/segments/s1", json={"description": "Yoshi 1"})
        assert resp.status_code == 200

    def test_delete_segment(self, client, db):
        s = Segment(id="s1", game_id="test_game", level_number=1,
                    start_type="entrance", start_ordinal=0,
                    end_type="goal", end_ordinal=0)
        db.upsert_segment(s)
        resp = client.delete("/api/segments/s1")
        assert resp.status_code == 200
        assert db.get_all_segments_with_model("test_game") == []


class TestFinalizeAndDiscardEndpoints:
    def test_finalize(self, client):
        client.app.state.session.finalize_run = AsyncMock(return_value=ActionResult(status=Status.OK))

        resp = client.post("/api/reference/finalize", json={"name": "My Run"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        client.app.state.session.finalize_run.assert_called_once_with("My Run")

    def test_finalize_default_name(self, client):
        client.app.state.session.finalize_run = AsyncMock(return_value=ActionResult(status=Status.OK))

        resp = client.post("/api/reference/finalize", json={})
        assert resp.status_code == 200
        client.app.state.session.finalize_run.assert_called_once_with("Untitled")

    def test_discard_run(self, client):
        client.app.state.session.discard_run = AsyncMock(return_value=ActionResult(status=Status.OK))

        resp = client.post("/api/reference/discard_run")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


class TestSaveAndFinishEndpoint:
    def test_save_and_finish(self, client):
        client.app.state.session.save_and_finish_run = AsyncMock(
            return_value=ActionResult(status=Status.OK)
        )

        resp = client.post("/api/reference/save_and_finish", json={"name": "My Run"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        client.app.state.session.save_and_finish_run.assert_called_once_with("My Run")

    def test_save_and_finish_default_name(self, client):
        client.app.state.session.save_and_finish_run = AsyncMock(
            return_value=ActionResult(status=Status.OK)
        )

        resp = client.post("/api/reference/save_and_finish", json={})
        assert resp.status_code == 200
        client.app.state.session.save_and_finish_run.assert_called_once_with("Untitled")


class TestResumeEndpoint:
    def test_resume(self, client):
        client.app.state.session.resume_reference = AsyncMock(
            return_value=ActionResult(status=Status.OK)
        )

        resp = client.post("/api/reference/resume")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


class TestDeleteCaptureSession:
    def test_delete_capture_session(self, client):
        client.app.state.session.delete_capture_session = AsyncMock(
            return_value=ActionResult(status=Status.OK)
        )

        resp = client.delete("/api/capture_sessions/sess_abc")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        client.app.state.session.delete_capture_session.assert_called_once_with("sess_abc")


class TestListCaptureSessions:
    def test_list_capture_sessions(self, client, db):
        db.create_capture_run("run1", "test_game", "Run 1")

        resp = client.get("/api/capture_sessions?run_id=run1")
        assert resp.status_code == 200
        assert "sessions" in resp.json()


class TestSpinrecEndpoint:
    def test_spinrec_exists(self, client, tmp_path):
        client.app.state.session.game_id = "testgame"
        client.app.state.session.data_dir = tmp_path
        rec_dir = tmp_path / "testgame" / "rec"
        rec_dir.mkdir(parents=True)
        (rec_dir / "ref_abc.spinrec").write_bytes(b"SREC")

        resp = client.get("/api/references/ref_abc/spinrec")
        assert resp.status_code == 200
        assert resp.json()["exists"] is True

    def test_spinrec_not_found(self, client):
        client.app.state.session.game_id = "testgame"
        resp = client.get("/api/references/ref_abc/spinrec")
        assert resp.status_code == 200
        assert resp.json()["exists"] is False


class TestReplayByRefId:
    def test_replay_start_with_ref_id(self, client, tmp_path):
        """Legacy fallback: no capture_sessions → uses {ref_id}.spinrec path."""
        client.app.state.session.game_id = "testgame"
        client.app.state.session.data_dir = tmp_path
        client.app.state.session.start_replay = AsyncMock(return_value=ActionResult(status=Status.STARTED))

        resp = client.post("/api/replay/start", json={"ref_id": "ref_abc", "speed": 1})
        assert resp.status_code == 200
        assert resp.json()["status"] == "started"
        expected_path = str(tmp_path / "testgame" / "rec" / "ref_abc.spinrec")
        client.app.state.session.start_replay.assert_called_once_with(expected_path, speed=1)

    def test_replay_start_missing_ref_id(self, client):
        resp = client.post("/api/replay/start", json={"speed": 0})
        assert resp.status_code == 400

    def test_replay_start_resolves_session_spinrec_path(self, client, db, tmp_path):
        """POST /replay/start uses the first session's spinrec_path, not {ref_id}.spinrec."""
        client.app.state.session.game_id = "test_game"
        client.app.state.session.data_dir = tmp_path
        client.app.state.session.start_replay = AsyncMock(return_value=ActionResult(status=Status.STARTED))

        session_spinrec = str(tmp_path / "test_game" / "rec" / "run1__sess001.spinrec")
        db.create_capture_run("run1", "test_game", "Run 1", draft=True)
        db.create_capture_session(
            session_id="sess_a", capture_run_id="run1",
            ordinal=1, spinrec_path=session_spinrec,
        )

        resp = client.post("/api/replay/start", json={"ref_id": "run1", "speed": 2})
        assert resp.status_code == 200
        # Must use the session's spinrec_path, NOT the old {ref_id}.spinrec construction
        client.app.state.session.start_replay.assert_called_once_with(session_spinrec, speed=2)


class TestListReferencesHasSpinrec:
    def test_list_references_includes_has_spinrec(self, client, db, tmp_path):
        """Legacy path fallback: run with no capture_sessions, file on disk."""
        client.app.state.session.data_dir = tmp_path
        db.create_capture_run("ref1", "test_game", "Run 1")
        rec_dir = tmp_path / "test_game" / "rec"
        rec_dir.mkdir(parents=True)
        (rec_dir / "ref1.spinrec").write_bytes(b"SREC")

        resp = client.get("/api/references")
        assert resp.status_code == 200
        refs = resp.json()["references"]
        assert len(refs) == 1
        assert refs[0]["has_spinrec"] is True

    def test_list_references_has_spinrec_false_when_missing(self, client, db, tmp_path):
        """Legacy path fallback: run with no capture_sessions, file absent."""
        client.app.state.session.data_dir = tmp_path
        db.create_capture_run("ref1", "test_game", "Run 1")

        resp = client.get("/api/references")
        assert resp.status_code == 200
        refs = resp.json()["references"]
        assert len(refs) == 1
        assert refs[0]["has_spinrec"] is False

    def test_list_references_has_spinrec_via_session(self, client, db, tmp_path):
        """Post-multi-session run: has_spinrec=True when session's spinrec_path exists."""
        client.app.state.session.data_dir = tmp_path
        # Finalized (draft=0) run with a capture_session whose file is on disk
        db.create_capture_run("ref2", "test_game", "Run 2")
        rec_dir = tmp_path / "test_game" / "rec"
        rec_dir.mkdir(parents=True)
        spinrec_file = rec_dir / "ref2__sess001.spinrec"
        spinrec_file.write_bytes(b"SREC")
        db.create_capture_session(
            session_id="sess_001", capture_run_id="ref2",
            ordinal=1, spinrec_path=str(spinrec_file),
        )
        # Promote to non-draft so list_capture_runs returns it
        db.promote_draft("ref2", "Run 2")

        resp = client.get("/api/references")
        assert resp.status_code == 200
        refs = resp.json()["references"]
        assert len(refs) == 1
        assert refs[0]["id"] == "ref2"
        assert refs[0]["has_spinrec"] is True


class TestReplayStop:
    def test_replay_stop_not_running(self, client):
        """POST /api/replay/stop returns 409 when no replay is active."""
        resp = client.post("/api/replay/stop")
        assert resp.status_code == 409
        assert resp.json()["detail"] == "not_replaying"

    def test_replay_stop_success(self, client):
        client.app.state.session.stop_replay = AsyncMock(
            return_value=ActionResult(status=Status.STOPPED)
        )
        resp = client.post("/api/replay/stop")
        assert resp.status_code == 200
        assert resp.json()["status"] == "stopped"


class TestReferenceSegments:
    def test_get_reference_segments(self, client, db):
        """GET /api/references/:id/segments returns segments for that reference."""
        db.create_capture_run("ref1", "test_game", "Run 1")
        s = Segment(id="s1", game_id="test_game", level_number=1,
                    start_type="entrance", start_ordinal=0,
                    end_type="goal", end_ordinal=0,
                    reference_id="ref1")
        db.upsert_segment(s)

        resp = client.get("/api/references/ref1/segments")
        assert resp.status_code == 200
        segs = resp.json()["segments"]
        assert len(segs) == 1
        assert segs[0]["id"] == "s1"

    def test_get_reference_segments_empty(self, client, db):
        """GET /api/references/:id/segments returns empty list when no segments."""
        db.create_capture_run("ref1", "test_game", "Run 1")
        resp = client.get("/api/references/ref1/segments")
        assert resp.status_code == 200
        assert resp.json()["segments"] == []

    def test_get_reference_segments_unknown_ref(self, client):
        """GET /api/references/:id/segments returns empty for unknown ref."""
        resp = client.get("/api/references/nonexistent/segments")
        assert resp.status_code == 200
        assert resp.json()["segments"] == []
