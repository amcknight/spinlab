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


class TestDraftEndpoints:
    def test_save_draft(self, client):
        # Inject draft state
        client.app.state.session.draft.run_id = "live_abc"
        client.app.state.session.draft.segments_count = 5
        client.app.state.session.save_draft = AsyncMock(return_value=ActionResult(status=Status.OK))

        resp = client.post("/api/references/draft/save", json={"name": "My Run"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_discard_draft(self, client):
        client.app.state.session.draft.run_id = "live_abc"
        client.app.state.session.discard_draft = AsyncMock(return_value=ActionResult(status=Status.OK))

        resp = client.post("/api/references/draft/discard")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


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


class TestListReferencesHasSpinrec:
    def test_list_references_includes_has_spinrec(self, client, db, tmp_path):
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
        client.app.state.session.data_dir = tmp_path
        db.create_capture_run("ref1", "test_game", "Run 1")

        resp = client.get("/api/references")
        assert resp.status_code == 200
        refs = resp.json()["references"]
        assert len(refs) == 1
        assert refs[0]["has_spinrec"] is False
