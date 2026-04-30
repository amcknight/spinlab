"""Tests for /api/speedrun start/stop routes."""
import pytest
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
from spinlab.db import Database
from spinlab.models import ActionResult, Status


GAME_ID = "test_game"


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    d.upsert_game(GAME_ID, "Test Game", "any%")
    return d


@pytest.fixture
def client(db):
    from spinlab.dashboard import create_app
    from conftest import make_test_config
    app = create_app(db=db, config=make_test_config())
    app.state.session.game_id = GAME_ID
    app.state.session.game_name = "Test Game"
    return TestClient(app)


class TestSpeedRunStart:
    def test_start_not_connected_returns_503(self, client):
        """POST /api/speedrun/start requires TCP connection."""
        resp = client.post("/api/speedrun/start")
        assert resp.status_code == 503
        assert resp.json()["detail"] == "not_connected"

    def test_start_success(self, client):
        client.app.state.session.start_speed_run = AsyncMock(
            return_value=ActionResult(status=Status.STARTED)
        )
        resp = client.post("/api/speedrun/start")
        assert resp.status_code == 200
        assert resp.json()["status"] == "started"


class TestSpeedRunStop:
    def test_stop_not_running_returns_409(self, client):
        """POST /api/speedrun/stop returns 409 when no speed run is active."""
        resp = client.post("/api/speedrun/stop")
        assert resp.status_code == 409
        assert resp.json()["detail"] == "not_running"

    def test_stop_success(self, client):
        client.app.state.session.stop_speed_run = AsyncMock(
            return_value=ActionResult(status=Status.STOPPED)
        )
        resp = client.post("/api/speedrun/stop")
        assert resp.status_code == 200
        assert resp.json()["status"] == "stopped"
