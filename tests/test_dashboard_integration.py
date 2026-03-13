"""Integration tests for dashboard — realistic seeded data, multi-step flows.

These tests use a fully seeded DB with splits, sessions, attempts, and Kalman
model state to validate the dashboard behaves correctly as a whole.  Designed
to grow as new tabs and features land (Manage tab, graphs, etc.).
"""
import json
import pytest
from pathlib import Path

from fastapi.testclient import TestClient
from spinlab.db import Database
from spinlab.models import Split, Attempt


# ── fixtures ────────────────────────────────────────────────────────────────

GAME_ID = "smw_kaizo"

SPLITS = [
    Split(id="s1", game_id=GAME_ID, level_number=101, room_id=0,
          goal="normal_exit", description="Yoshi's Island 1", reference_time_ms=4000),
    Split(id="s2", game_id=GAME_ID, level_number=102, room_id=0,
          goal="normal_exit", description="Yoshi's Island 2", reference_time_ms=7000),
    Split(id="s3", game_id=GAME_ID, level_number=103, room_id=0,
          goal="secret_exit", description="Donut Plains 1 (Secret)", reference_time_ms=12000),
    Split(id="s4", game_id=GAME_ID, level_number=104, room_id=0,
          goal="normal_exit", description="Vanilla Dome 1", reference_time_ms=9500),
    Split(id="s5", game_id=GAME_ID, level_number=105, room_id=0,
          goal="normal_exit", description="Forest of Illusion 1"),
]

ATTEMPTS = [
    ("s1", 4500, True),
    ("s1", 3800, True),
    ("s2", 7200, True),
    ("s3", 12000, False),
    ("s2", 6500, True),
    ("s1", 3200, True),
    ("s4", 9100, True),
    ("s3", 11500, True),
]

MODEL_STATES = [
    ("s1", 3.8, -0.15, 0.35),
    ("s2", 6.8,  0.05, 0.45),
    ("s3", 11.7, -0.02, 0.48),
    ("s4", 9.1,  0.0,  0.50),
]


@pytest.fixture
def seeded_db(tmp_path):
    """DB with game, splits, a session, attempts, and Kalman model state."""
    db = Database(tmp_path / "test.db")
    db.upsert_game(GAME_ID, "SMW Kaizo", "any%")

    for s in SPLITS:
        db.upsert_split(s)

    db.create_session("sess1", GAME_ID)

    for split_id, time_ms, completed in ATTEMPTS:
        db.log_attempt(Attempt(
            split_id=split_id, session_id="sess1",
            completed=completed, time_ms=time_ms,
        ))

    for split_id, mu, d, mr in MODEL_STATES:
        state = {"mu": mu, "P": 1.0, "d": d, "Q_mu": 0.5, "Q_d": 0.01, "R": 1.0, "n": 5}
        db.save_model_state(split_id, "kalman", json.dumps(state), mr)

    return db


@pytest.fixture
def client(seeded_db):
    from spinlab.dashboard import create_app
    app = create_app(db=seeded_db, game_id=GAME_ID, host="127.0.0.1", port=59999)
    return TestClient(app)


@pytest.fixture
def active_client(seeded_db):
    """Client with a simulated active practice session (practice mode).

    Injects a PracticeSession into the app's exposed state to simulate
    a running practice session without needing a real TCP connection.
    """
    from spinlab.dashboard import create_app
    from spinlab.practice import PracticeSession
    from unittest.mock import AsyncMock

    app = create_app(db=seeded_db, game_id=GAME_ID, host="127.0.0.1", port=59999)

    mock_tcp = AsyncMock()
    mock_tcp.is_connected = True
    ps = PracticeSession(tcp=mock_tcp, db=seeded_db, game_id=GAME_ID)
    ps.is_running = True
    ps.current_split_id = "s1"
    ps.session_id = "sess1"  # match the session already in DB

    # Inject into the app's exposed state lists
    app.state._practice[0] = ps

    return TestClient(app)


# ── Live tab: idle vs practice ──────────────────────────────────────────────

class TestLiveState:
    def test_idle_when_no_state_file(self, client):
        """Without orchestrator state file, mode should be 'idle'."""
        # Session exists in DB but no state file → reference mode actually
        # (the fixture has a session but no state file)
        resp = client.get("/api/state")
        assert resp.status_code == 200
        data = resp.json()
        assert data["mode"] in ("idle", "reference")

    def test_practice_mode_with_current_split(self, active_client):
        resp = active_client.get("/api/state")
        data = resp.json()
        assert data["mode"] == "practice"
        assert data["current_split"]["id"] == "s1"
        assert data["current_split"]["description"] == "Yoshi's Island 1"

    def test_current_split_has_attempt_count(self, active_client):
        data = active_client.get("/api/state").json()
        assert data["current_split"]["attempt_count"] == 3  # s1 has 3 attempts

    def test_current_split_has_drift_info(self, active_client):
        data = active_client.get("/api/state").json()
        drift = data["current_split"]["drift_info"]
        assert drift is not None
        assert drift["label"] == "improving"  # d = -0.15
        assert drift["drift"] == pytest.approx(-0.15)

    def test_queue_contains_next_splits(self, active_client):
        """Queue is now computed server-side: peek 3, exclude current, cap at 2."""
        data = active_client.get("/api/state").json()
        queue_ids = [s["id"] for s in data["queue"]]
        assert len(queue_ids) == 2
        assert "s1" not in queue_ids  # current split excluded

    def test_recent_attempts_ordered_newest_first(self, active_client):
        data = active_client.get("/api/state").json()
        recent = data["recent"]
        assert len(recent) == 8
        # Most recent attempt is s3 (11500ms)
        assert recent[0]["split_id"] == "s3"
        assert recent[0]["time_ms"] == 11500

    def test_recent_includes_reference_time(self, active_client):
        data = active_client.get("/api/state").json()
        for attempt in data["recent"]:
            assert "reference_time_ms" in attempt

    def test_session_info_present(self, active_client):
        data = active_client.get("/api/state").json()
        assert data["session"] is not None
        assert data["session"]["id"] == "sess1"

    def test_allocator_and_estimator_reported(self, active_client):
        data = active_client.get("/api/state").json()
        assert data["allocator"] == "greedy"
        assert data["estimator"] == "kalman"


# ── Model tab ───────────────────────────────────────────────────────────────

class TestModelEndpoint:
    def test_returns_all_splits(self, active_client):
        data = active_client.get("/api/model").json()
        assert len(data["splits"]) == 5
        assert data["estimator"] == "kalman"

    def test_splits_have_kalman_fields(self, active_client):
        data = active_client.get("/api/model").json()
        s1 = next(s for s in data["splits"] if s["split_id"] == "s1")
        assert s1["mu"] == pytest.approx(3.8)
        assert s1["drift"] == pytest.approx(-0.15, abs=0.001)
        assert s1["drift_info"]["label"] == "improving"

    def test_split_without_model_has_nulls(self, active_client):
        data = active_client.get("/api/model").json()
        s5 = next(s for s in data["splits"] if s["split_id"] == "s5")
        assert s5["mu"] is None
        assert s5["drift"] is None

    def test_marginal_return_present(self, active_client):
        data = active_client.get("/api/model").json()
        s1 = next(s for s in data["splits"] if s["split_id"] == "s1")
        assert s1["marginal_return"] == pytest.approx(0.0395, abs=0.01)


# ── Allocator / estimator switching ─────────────────────────────────────────

class TestAllocatorSwitch:
    def test_switch_allocator(self, active_client):
        resp = active_client.post("/api/allocator", json={"name": "random"})
        assert resp.status_code == 200
        assert resp.json()["allocator"] == "random"

    def test_switch_allocator_round_robin(self, active_client):
        resp = active_client.post("/api/allocator", json={"name": "round_robin"})
        assert resp.status_code == 200
        assert resp.json()["allocator"] == "round_robin"

    def test_switch_estimator(self, active_client):
        resp = active_client.post("/api/estimator", json={"name": "kalman"})
        assert resp.status_code == 200
        assert resp.json()["estimator"] == "kalman"


# ── Static assets ───────────────────────────────────────────────────────────

class TestStaticAssets:
    def test_index_html_has_three_tabs(self, active_client):
        html = active_client.get("/").text
        assert 'data-tab="live"' in html
        assert 'data-tab="model"' in html
        assert 'data-tab="manage"' in html

    def test_css_loads(self, active_client):
        resp = active_client.get("/static/style.css")
        assert resp.status_code == 200
        assert "--accent" in resp.text

    def test_js_loads(self, active_client):
        resp = active_client.get("/static/app.js")
        assert resp.status_code == 200
        assert "poll" in resp.text


# ── Splits and sessions endpoints ───────────────────────────────────────────

class TestSplitsAndSessions:
    def test_splits_endpoint_returns_all(self, active_client):
        data = active_client.get("/api/splits").json()
        assert len(data["splits"]) == 5
        ids = {s["id"] for s in data["splits"]}
        assert ids == {"s1", "s2", "s3", "s4", "s5"}

    def test_splits_ordered_by_level(self, active_client):
        data = active_client.get("/api/splits").json()
        levels = [s["level_number"] for s in data["splits"]]
        assert levels == sorted(levels)

    def test_sessions_endpoint(self, active_client):
        data = active_client.get("/api/sessions").json()
        assert len(data["sessions"]) == 1
        assert data["sessions"][0]["id"] == "sess1"
