"""Tests for dashboard API endpoints."""
import json
import pytest
from pathlib import Path

from fastapi.testclient import TestClient
from spinlab.db import Database
from spinlab.models import Split, Attempt


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    d.upsert_game("test_game", "Test Game", "any%")
    return d


@pytest.fixture
def client(db, tmp_path):
    from spinlab.dashboard import create_app
    # TCP will fail to connect (nothing listening) — dashboard stays in idle mode
    app = create_app(db=db, game_id="test_game", host="127.0.0.1", port=59999)
    return TestClient(app)


def test_api_state_no_session(client):
    resp = client.get("/api/state")
    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "idle"
    assert data["tcp_connected"] is False
    assert data["current_split"] is None


def test_api_state_idle_has_allocator(client):
    resp = client.get("/api/state")
    data = resp.json()
    assert "allocator" in data
    assert "estimator" in data


def test_api_splits_returns_all_with_model(client, db):
    s1 = Split(id="s1", game_id="test_game", level_number=1, room_id=0, goal="normal")
    s2 = Split(id="s2", game_id="test_game", level_number=2, room_id=0, goal="key")
    db.upsert_split(s1)
    db.upsert_split(s2)
    resp = client.get("/api/splits")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["splits"]) == 2


def test_api_sessions_returns_history(client, db):
    db.create_session("sess1", "test_game")
    db.end_session("sess1", 10, 8)
    resp = client.get("/api/sessions")
    assert resp.status_code == 200
    assert len(resp.json()["sessions"]) >= 1


def test_root_serves_html(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "SpinLab" in resp.text


def test_practice_start_not_connected(client):
    """Practice start should fail gracefully when TCP is not connected."""
    resp = client.post("/api/practice/start")
    assert resp.status_code == 200
    assert resp.json()["status"] == "not_connected"


def test_practice_stop_not_running(client):
    resp = client.post("/api/practice/stop")
    assert resp.status_code == 200
    assert resp.json()["status"] == "not_running"
