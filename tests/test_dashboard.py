"""Tests for dashboard API endpoints."""
import json
import pytest
from pathlib import Path

from fastapi.testclient import TestClient
from spinlab.db import Database
from spinlab.models import Split, Attempt, Rating


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    d.upsert_game("test_game", "Test Game", "any%")
    return d


@pytest.fixture
def state_file(tmp_path):
    return tmp_path / "orchestrator_state.json"


@pytest.fixture
def client(db, state_file, tmp_path):
    from spinlab.dashboard import create_app
    app = create_app(db=db, game_id="test_game", state_file=state_file)
    return TestClient(app)


def test_api_state_no_session(client):
    resp = client.get("/api/state")
    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "idle"
    assert data["current_split"] is None


def test_api_state_with_active_session(client, db, state_file):
    split = Split(id="s1", game_id="test_game", level_number=1,
                  room_id=0, goal="normal", description="Level 1",
                  reference_time_ms=5000)
    db.upsert_split(split)
    db.ensure_schedule("s1")
    db.create_session("sess1", "test_game")

    state_file.write_text(json.dumps({
        "session_id": "sess1",
        "current_split_id": "s1",
        "queue": [],
        "updated_at": "2026-03-12T15:30:00Z",
    }))

    resp = client.get("/api/state")
    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "practice"
    assert data["current_split"]["id"] == "s1"
    assert data["session"]["id"] == "sess1"


def test_api_splits_returns_all_with_schedule(client, db):
    s1 = Split(id="s1", game_id="test_game", level_number=1,
               room_id=0, goal="normal")
    s2 = Split(id="s2", game_id="test_game", level_number=2,
               room_id=0, goal="key")
    db.upsert_split(s1)
    db.upsert_split(s2)
    db.ensure_schedule("s1")
    db.ensure_schedule("s2")
    resp = client.get("/api/splits")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["splits"]) == 2
    assert "ease_factor" in data["splits"][0]


def test_api_sessions_returns_history(client, db):
    db.create_session("sess1", "test_game")
    db.end_session("sess1", 10, 8)
    db.create_session("sess2", "test_game")
    resp = client.get("/api/sessions")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["sessions"]) == 2
