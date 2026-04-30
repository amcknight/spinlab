"""Dashboard API tests — seeded DB, multi-step flows, error states.

Merged from test_dashboard.py + test_dashboard_integration.py.
The seeded DB is the primary fixture; lightweight fixtures for error-state tests.
"""
import asyncio
import json
import pytest
from pathlib import Path

from fastapi.testclient import TestClient
from spinlab.db import Database
from spinlab.models import Mode, Segment, Attempt


# -- fixtures ----------------------------------------------------------------

GAME_ID = "smw_kaizo"

SEGMENTS = [
    Segment(id="s1", game_id=GAME_ID, level_number=101,
            start_type="entrance", start_ordinal=0,
            end_type="goal", end_ordinal=0,
            description="Yoshi's Island 1"),
    Segment(id="s2", game_id=GAME_ID, level_number=102,
            start_type="entrance", start_ordinal=0,
            end_type="goal", end_ordinal=0,
            description="Yoshi's Island 2"),
    Segment(id="s3", game_id=GAME_ID, level_number=103,
            start_type="entrance", start_ordinal=0,
            end_type="goal", end_ordinal=0,
            description="Donut Plains 1 (Secret)"),
    Segment(id="s4", game_id=GAME_ID, level_number=104,
            start_type="entrance", start_ordinal=0,
            end_type="goal", end_ordinal=0,
            description="Vanilla Dome 1"),
    Segment(id="s5", game_id=GAME_ID, level_number=105,
            start_type="entrance", start_ordinal=0,
            end_type="goal", end_ordinal=0,
            description="Forest of Illusion 1"),
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
    """DB with game, segments, a session, attempts, and Kalman model state."""
    db = Database(tmp_path / "test.db")
    db.upsert_game(GAME_ID, "SMW Kaizo", "any%")

    states_dir = tmp_path / "states"
    states_dir.mkdir()
    for seg in SEGMENTS:
        state_file = states_dir / f"{seg.id}.mss"
        state_file.write_bytes(b"\x00" * 100)
        db.upsert_segment(seg)
        # TODO(Task 8): restore add_save_state on waypoint once get_all_segments_with_model
        # joins waypoint_save_states. state_path is NULL for all segments until Task 8.

    db.create_session("sess1", GAME_ID)

    for segment_id, time_ms, completed in ATTEMPTS:
        db.log_attempt(Attempt(
            segment_id=segment_id, session_id="sess1",
            completed=completed, time_ms=time_ms,
        ))

    gold_times = {"s1": 3.2, "s2": 6.5, "s3": 11.5, "s4": 9.1}
    for segment_id, mu, d, mr in MODEL_STATES:
        state = {"mu": mu, "P": 1.0, "d": d, "Q_mu": 0.5, "Q_d": 0.01, "R": 1.0, "n": 5,
                 "gold": gold_times[segment_id], "n_completed": 3, "n_attempts": 3}
        output = {
            "total": {"expected_ms": mu * 1000, "ms_per_attempt": mr * 1000, "floor_ms": mu * 800},
            "clean": {"expected_ms": None, "ms_per_attempt": None, "floor_ms": None},
        }
        db.save_model_state(segment_id, "kalman", json.dumps(state), json.dumps(output))

    return db


@pytest.fixture
def client(seeded_db):
    from spinlab.dashboard import create_app
    from conftest import make_test_config
    app = create_app(db=seeded_db, config=make_test_config())
    app.state.session.game_id = GAME_ID
    app.state.session.game_name = "SMW Kaizo"
    return TestClient(app)


@pytest.fixture
def active_client(seeded_db):
    """Client with a simulated active practice session."""
    from spinlab.dashboard import create_app
    from spinlab.practice import PracticeSession
    from unittest.mock import AsyncMock

    from conftest import make_test_config
    app = create_app(db=seeded_db, config=make_test_config())
    app.state.session.game_id = GAME_ID
    app.state.session.game_name = "SMW Kaizo"

    mock_tcp = AsyncMock()
    mock_tcp.is_connected = True
    ps = PracticeSession(tcp=mock_tcp, db=seeded_db, game_id=GAME_ID)
    ps.is_running = True
    ps.current_segment_id = "s1"
    ps.session_id = "sess1"

    app.state.session.practice_session = ps
    app.state.session.mode = Mode.PRACTICE

    return TestClient(app)


@pytest.fixture
def bare_client(tmp_path):
    """Client with minimal DB and no game loaded — for error-state tests."""
    from spinlab.dashboard import create_app
    from conftest import make_test_config
    db = Database(tmp_path / "test.db")
    db.upsert_game("test_game", "Test Game", "any%")
    app = create_app(db=db, config=make_test_config())
    app.state.session.game_id = "test_game"
    app.state.session.game_name = "Test Game"
    return TestClient(app)


@pytest.fixture
def no_game_client(tmp_path):
    """Client with no game context set."""
    from spinlab.dashboard import create_app
    from conftest import make_test_config
    db = Database(tmp_path / "test.db")
    db.upsert_game("test_game", "Test Game", "any%")
    app = create_app(db=db, config=make_test_config())
    return TestClient(app)


def _sync_switch(app, game_id, game_name):
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(app.state.session.switch_game(game_id, game_name))
    finally:
        loop.close()


# -- API state ---------------------------------------------------------------

class TestApiState:
    def test_idle_state(self, client):
        resp = client.get("/api/state")
        assert resp.status_code == 200
        data = resp.json()
        assert data["mode"] in ("idle", "reference")
        assert data["tcp_connected"] is False

    def test_no_game_loaded(self, no_game_client):
        data = no_game_client.get("/api/state").json()
        assert data["game_id"] is None
        assert data["game_name"] is None
        assert data["allocator_weights"] is None

    def test_practice_mode_with_current_segment(self, active_client):
        data = active_client.get("/api/state").json()
        assert data["mode"] == "practice"
        assert data["current_segment"]["id"] == "s1"
        assert data["current_segment"]["description"] == "Yoshi's Island 1"
        assert data["current_segment"]["attempt_count"] == 3
        assert "kalman" in data["current_segment"]["model_outputs"]

    def test_recent_attempts_ordered_newest_first(self, active_client):
        data = active_client.get("/api/state").json()
        recent = data["recent"]
        assert len(recent) == 8
        assert recent[0]["segment_id"] == "s3"
        assert recent[0]["time_ms"] == 11500

    def test_session_info_present(self, active_client):
        data = active_client.get("/api/state").json()
        assert data["session"]["id"] == "sess1"

    def test_allocator_and_estimator_reported(self, active_client):
        data = active_client.get("/api/state").json()
        assert isinstance(data["allocator_weights"], dict)
        assert sum(data["allocator_weights"].values()) == 100
        assert data["estimator"] == "kalman"


# -- Model tab ---------------------------------------------------------------

class TestModelEndpoint:
    def test_returns_all_segments_with_model(self, active_client):
        data = active_client.get("/api/model").json()
        assert len(data["segments"]) == 5
        assert data["estimator"] == "kalman"

        s1 = next(s for s in data["segments"] if s["segment_id"] == "s1")
        kalman = s1["model_outputs"]["kalman"]
        assert kalman["total"]["expected_ms"] == pytest.approx(3800, abs=100)
        assert kalman["total"]["ms_per_attempt"] is not None

    def test_segment_without_model_has_empty_outputs(self, active_client):
        data = active_client.get("/api/model").json()
        s5 = next(s for s in data["segments"] if s["segment_id"] == "s5")
        assert s5["model_outputs"] == {}

    def test_segment_has_start_end_types(self, active_client):
        data = active_client.get("/api/model").json()
        s1 = next(s for s in data["segments"] if s["segment_id"] == "s1")
        assert s1["start_type"] == "entrance"
        assert s1["end_type"] == "goal"

    def test_practiced_segment_has_gold(self, active_client):
        data = active_client.get("/api/model").json()
        s1 = next(s for s in data["segments"] if s["segment_id"] == "s1")
        assert s1["gold_ms"] is not None

    def test_model_response_matches_frontend_types(self, active_client):
        """Verify /api/model response structure matches frontend TypeScript types.

        The frontend expects: segments[].model_outputs[name].total.expected_ms
        NOT: segments[].model_outputs[name].expected_time_ms (old flat structure)
        """
        resp = active_client.get("/api/model")
        assert resp.status_code == 200
        data = resp.json()

        # Top-level keys match ModelData interface
        assert set(data.keys()) == {"estimator", "estimators", "allocator_weights", "segments"}

        if data["segments"]:
            seg = data["segments"][0]
            # Keys match ModelSegment interface
            expected_keys = {
                "segment_id", "description", "level_number",
                "start_type", "start_ordinal", "end_type", "end_ordinal",
                "selected_model", "model_outputs",
                "n_completed", "n_attempts", "gold_ms", "clean_gold_ms",
            }
            assert set(seg.keys()) == expected_keys

            # model_outputs has nested total/clean structure
            if seg["model_outputs"]:
                output = next(iter(seg["model_outputs"].values()))
                assert set(output.keys()) == {"total", "clean"}
                assert set(output["total"].keys()) == {"expected_ms", "ms_per_attempt", "floor_ms"}


# -- Allocator / estimator switching -----------------------------------------

class TestAllocatorSwitch:
    def test_set_allocator_weights(self, active_client):
        resp = active_client.post("/api/allocator-weights", json={"random": 100})
        assert resp.status_code == 200
        assert resp.json()["weights"] == {"random": 100}

    def test_set_allocator_weights_mixed(self, active_client):
        resp = active_client.post("/api/allocator-weights", json={"greedy": 50, "round_robin": 50})
        assert resp.status_code == 200
        assert resp.json()["weights"] == {"greedy": 50, "round_robin": 50}

    def test_set_allocator_weights_invalid_sum(self, active_client):
        resp = active_client.post("/api/allocator-weights", json={"random": 50})
        assert resp.status_code == 400

    def test_set_allocator_weights_missing_body(self, active_client):
        resp = active_client.post("/api/allocator-weights", json={"name": "random"})
        assert resp.status_code == 400

    def test_switch_estimator(self, active_client):
        resp = active_client.post("/api/estimator", json={"name": "kalman"})
        assert resp.status_code == 200
        assert resp.json()["estimator"] == "kalman"


# -- Error states (503/409) --------------------------------------------------

class TestErrorStates:
    def test_practice_start_not_connected(self, bare_client):
        resp = bare_client.post("/api/practice/start")
        assert resp.status_code == 503
        assert resp.json()["detail"] == "not_connected"

    def test_practice_stop_not_running(self, bare_client):
        resp = bare_client.post("/api/practice/stop")
        assert resp.status_code == 409
        assert resp.json()["detail"] == "not_running"

    def test_reference_start_not_connected(self, bare_client):
        resp = bare_client.post("/api/reference/start")
        assert resp.status_code == 503
        assert resp.json()["detail"] == "not_connected"

    def test_reference_stop_not_in_reference(self, bare_client):
        resp = bare_client.post("/api/reference/stop")
        assert resp.status_code == 409
        assert resp.json()["detail"] == "not_in_reference"

    def test_launch_emulator_no_config(self, bare_client):
        resp = bare_client.post("/api/emulator/launch")
        assert resp.status_code == 400
        assert "Emulator not found" in resp.json()["detail"]


# -- Game switching ----------------------------------------------------------

class TestGameSwitching:
    def test_switch_game_sets_context(self, bare_client):
        _sync_switch(bare_client.app, "new_checksum", "New Game")
        data = bare_client.get("/api/state").json()
        assert data["game_id"] == "new_checksum"
        assert data["game_name"] == "New Game"

    def test_switch_game_same_id_is_noop(self, bare_client):
        _sync_switch(bare_client.app, "test_game", "Test Game")
        assert bare_client.get("/api/state").json()["mode"] == "idle"

    def test_switch_game_resets_scheduler(self, bare_client):
        bare_client.get("/api/state")
        assert bare_client.app.state.session.scheduler is not None
        _sync_switch(bare_client.app, "other_game", "Other Game")
        assert bare_client.app.state.session.scheduler is None


# -- Misc dashboard behavior ------------------------------------------------

def test_reset_clears_mode_state(bare_client):
    db = bare_client.app.state.session.db
    db.create_session("s1", "test_game")
    db.end_session("s1", 5, 3)
    resp = bare_client.post("/api/reset")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_practice_stop_clears_stale_mode(bare_client):
    bare_client.app.state.session.mode = Mode.PRACTICE
    resp = bare_client.post("/api/practice/stop")
    assert resp.status_code == 200
    assert resp.json()["status"] == "stopped"
    assert bare_client.app.state.session.mode == Mode.IDLE


def test_fresh_db_reference_start_creates_game(tmp_path):
    from unittest.mock import AsyncMock, PropertyMock, patch
    from spinlab.dashboard import create_app

    from conftest import make_test_config
    fresh_db = Database(tmp_path / "fresh.db")
    app = create_app(db=fresh_db, config=make_test_config())
    _sync_switch(app, "test_game", "Test Game")
    with patch.object(type(app.state.tcp), "is_connected", new_callable=PropertyMock, return_value=True), \
         patch.object(app.state.tcp, "send", new_callable=AsyncMock):
        c = TestClient(app)
        resp = c.post("/api/reference/start")
        assert resp.status_code == 200
        assert resp.json()["status"] == "started"


# -- Segments and sessions ---------------------------------------------------

class TestSegmentsAndSessions:
    def test_segments_endpoint_returns_all_ordered(self, active_client):
        data = active_client.get("/api/segments").json()
        assert len(data["segments"]) == 5
        ids = {s["id"] for s in data["segments"]}
        assert ids == {"s1", "s2", "s3", "s4", "s5"}
        levels = [s["level_number"] for s in data["segments"]]
        assert levels == sorted(levels)

    def test_sessions_endpoint(self, active_client):
        data = active_client.get("/api/sessions").json()
        assert len(data["sessions"]) == 1
        assert data["sessions"][0]["id"] == "sess1"


# -- Segment history ---------------------------------------------------------

def test_segment_history_returns_attempts_and_curves(client):
    resp = client.get("/api/segments/s1/history")
    assert resp.status_code == 200
    data = resp.json()
    assert data["segment_id"] == "s1"
    assert data["description"] == "Yoshi's Island 1"
    # s1 has 3 completed attempts in ATTEMPTS fixture (4500, 3800, 3200)
    assert len(data["attempts"]) == 3
    assert data["attempts"][0]["attempt_number"] == 1
    assert data["attempts"][0]["time_ms"] == 4500
    assert data["attempts"][2]["time_ms"] == 3200
    # Every registered estimator should have curves
    curves = data["estimator_curves"]
    assert "kalman" in curves
    for est_name, est_curves in curves.items():
        assert "total" in est_curves
        assert "clean" in est_curves
        assert len(est_curves["total"]["expected_ms"]) == 3


def test_segment_history_excludes_incomplete(seeded_db, client):
    """s3 has one incomplete (12000, False) and one complete (11500, True)."""
    resp = client.get("/api/segments/s3/history")
    assert resp.status_code == 200
    data = resp.json()
    # Only the completed attempt should appear
    assert len(data["attempts"]) == 1
    assert data["attempts"][0]["time_ms"] == 11500


def test_segment_history_unknown_segment(client):
    resp = client.get("/api/segments/nonexistent/history")
    assert resp.status_code == 404


def test_segment_history_no_completed_attempts(seeded_db, client):
    """s5 has no attempts at all."""
    resp = client.get("/api/segments/s5/history")
    assert resp.status_code == 200
    data = resp.json()
    assert data["attempts"] == []
    for est_curves in data["estimator_curves"].values():
        assert est_curves["total"]["expected_ms"] == []
        assert est_curves["clean"]["expected_ms"] == []


# -- GET /roms ---------------------------------------------------------------

class TestRomsEndpoint:
    def test_roms_no_rom_dir(self, bare_client):
        """GET /api/roms returns empty list when rom_dir is not configured."""
        resp = bare_client.get("/api/roms")
        assert resp.status_code == 200
        data = resp.json()
        assert data["roms"] == []
        assert "error" in data

    def test_roms_with_rom_dir(self, tmp_path):
        """GET /api/roms lists ROM files from rom_dir."""
        rom_dir = tmp_path / "roms"
        rom_dir.mkdir()
        (rom_dir / "Game A.smc").write_bytes(b"\x00")
        (rom_dir / "Game B.sfc").write_bytes(b"\x00")
        (rom_dir / "readme.txt").write_text("not a rom")

        from spinlab.dashboard import create_app
        from conftest import make_test_config
        db = Database(tmp_path / "test.db")
        app = create_app(db=db, config=make_test_config(rom_dir=rom_dir))
        client = TestClient(app)

        resp = client.get("/api/roms")
        assert resp.status_code == 200
        roms = resp.json()["roms"]
        assert len(roms) == 2
        assert "Game A.smc" in roms
        assert "Game B.sfc" in roms
        assert "readme.txt" not in roms


# -- POST /shutdown ----------------------------------------------------------

def test_shutdown_returns_shutting_down(bare_client):
    """POST /api/shutdown calls session.shutdown and returns status."""
    from unittest.mock import AsyncMock, patch
    bare_client.app.state.session.shutdown = AsyncMock()
    with patch("signal.raise_signal"):
        resp = bare_client.post("/api/shutdown")
    assert resp.status_code == 200
    assert resp.json()["status"] == "shutting_down"
    bare_client.app.state.session.shutdown.assert_called_once()


# -- GET/POST /estimator-params ----------------------------------------------

class TestEstimatorParams:
    def test_get_estimator_params_no_game(self, no_game_client):
        resp = no_game_client.get("/api/estimator-params")
        assert resp.status_code == 200
        data = resp.json()
        assert data["estimator"] is None
        assert data["params"] == []

    def test_get_estimator_params_returns_declared(self, active_client):
        resp = active_client.get("/api/estimator-params")
        assert resp.status_code == 200
        data = resp.json()
        assert data["estimator"] == "kalman"
        assert len(data["params"]) > 0
        # Each param has required fields
        for p in data["params"]:
            assert "name" in p
            assert "default" in p
            assert "value" in p

    def test_post_estimator_params_roundtrip(self, active_client):
        """POST /api/estimator-params saves params, GET reads them back."""
        resp = active_client.post(
            "/api/estimator-params",
            json={"params": {"D0": 1.5}},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

        resp2 = active_client.get("/api/estimator-params")
        data = resp2.json()
        d0 = next(p for p in data["params"] if p["name"] == "D0")
        assert d0["value"] == 1.5

    def test_post_estimator_params_unknown_param(self, active_client):
        resp = active_client.post(
            "/api/estimator-params",
            json={"params": {"nonexistent_param": 42}},
        )
        assert resp.status_code == 400
        assert "Unknown param" in resp.json()["detail"]
