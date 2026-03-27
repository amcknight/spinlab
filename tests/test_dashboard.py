"""Tests for dashboard API endpoints."""
import asyncio
import json
import pytest
from pathlib import Path

from fastapi.testclient import TestClient
from spinlab.db import Database
from spinlab.models import Mode, Segment, SegmentVariant, Attempt


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    d.upsert_game("test_game", "Test Game", "any%")
    return d


@pytest.fixture
def client(db, tmp_path):
    from spinlab.dashboard import create_app
    app = create_app(db=db, host="127.0.0.1", port=59999)
    app.state.session.game_id = "test_game"
    app.state.session.game_name = "Test Game"
    return TestClient(app)


@pytest.fixture
def client_no_game(db, tmp_path):
    from spinlab.dashboard import create_app
    app = create_app(db=db, host="127.0.0.1", port=59999)
    return TestClient(app)


def test_api_state_no_session(client):
    resp = client.get("/api/state")
    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "idle"
    assert data["tcp_connected"] is False
    assert data["current_segment"] is None


def test_api_state_idle_has_allocator(client):
    resp = client.get("/api/state")
    data = resp.json()
    assert "allocator" in data
    assert "estimator" in data


def test_api_state_no_game_loaded(client_no_game):
    resp = client_no_game.get("/api/state")
    assert resp.status_code == 200
    data = resp.json()
    assert data["game_id"] is None
    assert data["game_name"] is None
    assert data["allocator"] is None


def test_api_segments_returns_all_with_model(client, db):
    s1 = Segment(id="s1", game_id="test_game", level_number=1,
                 start_type="entrance", start_ordinal=0,
                 end_type="goal", end_ordinal=0)
    s2 = Segment(id="s2", game_id="test_game", level_number=2,
                 start_type="entrance", start_ordinal=0,
                 end_type="goal", end_ordinal=0)
    db.upsert_segment(s1)
    db.upsert_segment(s2)
    resp = client.get("/api/segments")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["segments"]) == 2


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
    assert resp.status_code == 503
    assert resp.json()["detail"] == "not_connected"


def test_practice_stop_not_running(client):
    resp = client.post("/api/practice/stop")
    assert resp.status_code == 409
    assert resp.json()["detail"] == "not_running"


def test_reference_start_not_connected(client):
    resp = client.post("/api/reference/start")
    assert resp.status_code == 503
    assert resp.json()["detail"] == "not_connected"


def test_reference_stop_not_in_reference(client):
    resp = client.post("/api/reference/stop")
    assert resp.status_code == 409
    assert resp.json()["detail"] == "not_in_reference"


def test_reset_clears_mode_state(client, db):
    db.create_session("s1", "test_game")
    db.end_session("s1", 5, 3)
    resp = client.post("/api/reset")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_launch_emulator_no_config(client):
    resp = client.post("/api/emulator/launch")
    assert resp.status_code == 400
    assert "Emulator not found" in resp.json()["detail"]


def _sync_switch(app, game_id, game_name):
    """Helper to call async switch_game from sync test code."""
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(app.state.session.switch_game(game_id, game_name))
    finally:
        loop.close()


def test_fresh_db_reference_start_creates_game(tmp_path):
    """Reference start on a fresh DB should not FK-crash (game row auto-created)."""
    from unittest.mock import AsyncMock, PropertyMock, patch
    from spinlab.dashboard import create_app

    fresh_db = Database(tmp_path / "fresh.db")
    app = create_app(db=fresh_db, host="127.0.0.1", port=59999)
    # Simulate game context (normally set by rom_info event)
    _sync_switch(app, "test_game", "Test Game")
    # Simulate TCP connected so reference start doesn't bail early
    with patch.object(type(app.state.tcp), "is_connected", new_callable=PropertyMock, return_value=True), \
         patch.object(app.state.tcp, "send", new_callable=AsyncMock):
        c = TestClient(app)
        resp = c.post("/api/reference/start")
        assert resp.status_code == 200
        assert resp.json()["status"] == "started"


def test_practice_stop_clears_stale_mode(client):
    """If practice self-terminates, stop should still reset mode to idle."""
    # Manually set mode to practice (simulating a self-terminated session)
    client.app.state.session.mode = Mode.PRACTICE
    resp = client.post("/api/practice/stop")
    assert resp.status_code == 200
    assert resp.json()["status"] == "stopped"
    assert client.app.state.session.mode == Mode.IDLE


def test_switch_game_sets_context(client, db):
    """Switching game updates game_id and game_name."""
    app = client.app
    _sync_switch(app, "new_checksum", "New Game")
    resp = client.get("/api/state")
    data = resp.json()
    assert data["game_id"] == "new_checksum"
    assert data["game_name"] == "New Game"


def test_switch_game_same_id_is_noop(client, db):
    """Switching to the same game should be a no-op."""
    app = client.app
    _sync_switch(app, "test_game", "Test Game")
    # mode should still be whatever it was (not reset to idle)
    resp = client.get("/api/state")
    assert resp.json()["mode"] == "idle"


def test_switch_game_resets_scheduler(client, db):
    """Switching game should invalidate cached scheduler."""
    app = client.app
    # Access scheduler to cache it
    client.get("/api/state")
    assert app.state.session.scheduler is not None
    # Switch game
    _sync_switch(app, "other_game", "Other Game")
    assert app.state.session.scheduler is None
