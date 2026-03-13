"""Tests for reference and split management API endpoints."""
import json
import pytest
from pathlib import Path

from fastapi.testclient import TestClient
from spinlab.db import Database
from spinlab.models import Split


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    d.upsert_game("test_game", "Test Game", "any%")
    return d


@pytest.fixture
def client(db, tmp_path):
    from spinlab.dashboard import create_app
    app = create_app(db=db, host="127.0.0.1", port=59999)
    app.state._game_id[0] = "test_game"
    app.state._game_name[0] = "Test Game"
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
        s = Split(id="s1", game_id="test_game", level_number=1, room_id=0,
                  goal="normal", reference_id="ref1")
        db.upsert_split(s)
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


class TestSplitEditEndpoints:
    def test_patch_split(self, client, db):
        s = Split(id="s1", game_id="test_game", level_number=1, room_id=0, goal="normal")
        db.upsert_split(s)
        resp = client.patch("/api/splits/s1", json={"description": "Yoshi 1"})
        assert resp.status_code == 200

    def test_delete_split(self, client, db):
        s = Split(id="s1", game_id="test_game", level_number=1, room_id=0, goal="normal")
        db.upsert_split(s)
        resp = client.delete("/api/splits/s1")
        assert resp.status_code == 200
        assert db.get_all_splits_with_model("test_game") == []


class TestImportManifest:
    def test_import_manifest(self, client, tmp_path):
        import yaml
        manifest = {
            "game_id": "test_game",
            "category": "any%",
            "splits": [
                {"id": "test_game:1:0:normal", "level_number": 1, "room_id": 0,
                 "goal": "normal", "name": "L1", "reference_time_ms": 5000},
            ],
        }
        manifest_path = tmp_path / "test_manifest.yaml"
        with manifest_path.open("w") as f:
            yaml.dump(manifest, f)
        resp = client.post(
            "/api/import-manifest",
            json={"path": str(manifest_path)},
        )
        assert resp.status_code == 200
        assert resp.json()["splits_imported"] == 1
