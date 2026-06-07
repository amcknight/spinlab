"""Dashboard API tests — seeded DB, multi-step flows, error states.

Merged from test_dashboard.py + test_dashboard_integration.py.
The seeded DB is the primary fixture; lightweight fixtures for error-state tests.
"""
import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from spinlab.db import Database
from spinlab.models import Attempt, Mode, Segment

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

MODEL_OUTPUTS = [
    # (segment_id, expected_seconds, ms_per_attempt_seconds, floor_factor)
    ("s1", 3.8, -0.15, 0.35),
    ("s2", 6.8,  0.05, 0.45),
    ("s3", 11.7, -0.02, 0.48),
    ("s4", 9.1,  0.0,  0.50),
]


@pytest.fixture
def seeded_db(tmp_path):
    """DB with game, segments, a session, attempts, and seeded model_state rows.

    state_json is intentionally opaque here — the /api/model route reads
    output_json. Real production state shape is owned by SamplerState; tests
    that care about state round-trips live in test_em_suite_sampler.py.
    """
    from tests.factories import stamp_reference_traversal

    db = Database(tmp_path / "test.db")
    db.upsert_game(GAME_ID, "SMW Kaizo", "any%")

    # An active reference run that traversed every segment. Model views are
    # run-scoped (reference-run-selector): they show only segments the active
    # run traversed, so the fixture must make all SEGMENTS members of it.
    ref_id = f"{GAME_ID}:ref"
    db.create_capture_run(ref_id, GAME_ID, "Ref", kind="live")
    db.promote_draft(ref_id, "Ref")
    db.set_active_capture_run(ref_id)

    states_dir = tmp_path / "states"
    states_dir.mkdir()
    for seg in SEGMENTS:
        state_file = states_dir / f"{seg.id}.mss"
        state_file.write_bytes(b"\x00" * 100)
        db.upsert_segment(seg)
        # Membership via a DIED traversal: makes the segment a member of the
        # active run without adding a COMPLETED attempt, so segment-history's
        # completed-attempt counts (asserted below) stay exactly as ATTEMPTS.
        stamp_reference_traversal(db, seg.id, ref_id, survived=False)

    db.create_session("sess1", GAME_ID)

    for segment_id, time_ms, completed in ATTEMPTS:
        db.log_attempt(Attempt(
            segment_id=segment_id, session_id="sess1",
            completed=completed, time_ms=time_ms,
        ))

    for segment_id, expected_s, mr_s, floor_factor in MODEL_OUTPUTS:
        state = {"n_completed": 3, "n_attempts": 3}
        output = {
            "total": {"expected_ms": expected_s * 1000, "ms_per_attempt": mr_s * 1000,
                      "floor_ms": expected_s * 1000 * floor_factor},
            "clean": {"expected_ms": None, "ms_per_attempt": None, "floor_ms": None},
        }
        db.save_model_state(segment_id, "em_suite_sampler", json.dumps(state), json.dumps(output))

    return db


@pytest.fixture
def client(seeded_db):
    from tests.conftest import make_test_config

    from spinlab.dashboard import create_app
    app = create_app(db=seeded_db, config=make_test_config())
    app.state.session.game_id = GAME_ID
    app.state.session.game_name = "SMW Kaizo"
    return TestClient(app)


@pytest.fixture
def active_client(seeded_db):
    """Client with a simulated active practice session."""
    from unittest.mock import AsyncMock

    from tests.conftest import make_test_config

    from spinlab.dashboard import create_app
    from spinlab.practice import PracticeSession
    app = create_app(db=seeded_db, config=make_test_config())
    app.state.session.game_id = GAME_ID
    app.state.session.game_name = "SMW Kaizo"

    mock_emu = AsyncMock()
    mock_emu.is_connected = True
    from spinlab.scheduler import Scheduler
    ps = PracticeSession(
        emu=mock_emu, db=seeded_db, game_id=GAME_ID, session_id="sess1",
        scheduler=Scheduler(seeded_db, GAME_ID),
    )
    ps.is_running = True
    ps.current_segment_id = "s1"

    app.state.session.practice_session = ps
    app.state.session.mode = Mode.PRACTICE

    return TestClient(app)


@pytest.fixture
def bare_client(tmp_path):
    """Client with minimal DB and no game loaded — for error-state tests."""
    from tests.conftest import make_test_config

    from spinlab.dashboard import create_app
    db = Database(tmp_path / "test.db")
    db.upsert_game("test_game", "Test Game", "any%")
    app = create_app(db=db, config=make_test_config())
    app.state.session.game_id = "test_game"
    app.state.session.game_name = "Test Game"
    return TestClient(app)


@pytest.fixture
def no_game_client(tmp_path):
    """Client with no game context set."""
    from tests.conftest import make_test_config

    from spinlab.dashboard import create_app
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
        assert data["emu_connected"] is False

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
        assert "em_suite_sampler" in data["current_segment"]["model_outputs"]

    def test_recent_attempts_ordered_newest_first(self, active_client):
        from spinlab.db.attempts import RECENT_ATTEMPTS_DB_LIMIT
        data = active_client.get("/api/state").json()
        recent = data["recent"]
        assert len(recent) == RECENT_ATTEMPTS_DB_LIMIT
        assert recent[0]["segment_id"] == "s3"
        assert recent[0]["time_ms"] == 11500

    def test_session_info_present(self, active_client):
        data = active_client.get("/api/state").json()
        assert data["session"]["id"] == "sess1"

    def test_allocator_and_estimator_reported(self, active_client):
        data = active_client.get("/api/state").json()
        assert isinstance(data["allocator_weights"], dict)
        assert sum(data["allocator_weights"].values()) == 100
        assert data["estimator"] == "em_suite_sampler"


# -- Model tab ---------------------------------------------------------------

class TestModelEndpoint:
    def test_returns_all_segments_with_model(self, active_client):
        data = active_client.get("/api/model").json()
        assert len(data["segments"]) == 5
        assert data["estimator"] == "em_suite_sampler"

        s1 = next(s for s in data["segments"] if s["segment_id"] == "s1")
        out = s1["model_outputs"]["em_suite_sampler"]
        assert out["total"]["expected_ms"] == pytest.approx(3800, abs=100)
        assert out["total"]["ms_per_attempt"] is not None

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
        assert set(data.keys()) == {"estimator", "allocator_weights", "segments"}

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
                assert set(output.keys()) == {"total", "clean", "extras", "practice_gain_ms"}
                assert set(output["total"].keys()) == {"expected_ms", "ms_per_attempt", "floor_ms"}

    def test_api_model_segment_carries_practice_gain_key(self, active_client):
        """Contract guard: practice_gain_ms must be present in every model output.

        The frontend Practice column reads this key; if it is dropped from
        ModelOutput.to_dict() the column breaks silently. This test fails loudly
        instead. The value may be None (slope ungated) — only the key is asserted.
        """
        resp = active_client.get("/api/model")
        assert resp.status_code == 200
        segments = resp.json()["segments"]
        assert segments, "expected at least one model segment"
        for seg in segments:
            for _name, out in seg["model_outputs"].items():
                assert "practice_gain_ms" in out


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
        # Body must be dict[str, int]; {"name": "random"} has a non-int value
        # which Pydantic rejects at the validation layer (422), not the
        # business-rule layer (400).
        resp = active_client.post("/api/allocator-weights", json={"name": "random"})
        assert resp.status_code == 422



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
        from unittest.mock import patch
        # Patch out RA-running check so we hit the missing-retroarch_path 400.
        with patch("spinlab.routes.system._retroarch_already_running", return_value=False):
            resp = bare_client.post("/api/emulator/launch", json={"rom": "x.smc"})
        assert resp.status_code == 400
        assert "retroarch_path" in resp.json()["detail"]


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
    from unittest.mock import PropertyMock, patch

    from tests.conftest import make_test_config

    from spinlab.dashboard import create_app
    fresh_db = Database(tmp_path / "fresh.db")
    app = create_app(db=fresh_db, config=make_test_config())
    _sync_switch(app, "test_game", "Test Game")
    with patch.object(type(app.state.emu), "is_connected", new_callable=PropertyMock, return_value=True):
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
    # Single-model world: only em_suite_sampler; per-attempt series are empty
    # (the matrix endpoint is the real time-series view for em_suite).
    curves = data["estimator_curves"]
    assert "em_suite_sampler" in curves
    for est_name, est_curves in curves.items():
        assert "total" in est_curves
        assert "clean" in est_curves
        assert est_curves["total"]["expected_ms"] == []
        assert est_curves["clean"]["expected_ms"] == []


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


def test_segment_history_returns_selected_model(client):
    """selected_model mirrors the scheduler's active estimator name."""
    resp = client.get("/api/segments/s1/history")
    assert resp.status_code == 200
    data = resp.json()
    # The seeded client uses the default scheduler — "em_suite_sampler" is the
    # default estimator, matching test_recent_attempts_ordered* assertions.
    assert data["selected_model"] == "em_suite_sampler"


def test_segment_history_returns_null_selected_model_when_no_game(seeded_db):
    """selected_model is None (not ``''``) when no game is loaded."""
    from spinlab.dashboard import create_app
    from tests.conftest import make_test_config

    # Build a client whose session has no game_id set. The seeded DB
    # still contains s1's segment row, so the segment lookup itself
    # succeeds; only the scheduler-derived selected_model is absent.
    app = create_app(db=seeded_db, config=make_test_config())
    # Explicitly do NOT set app.state.session.game_id.
    client = TestClient(app)

    resp = client.get("/api/segments/s1/history")
    assert resp.status_code == 200
    assert resp.json()["selected_model"] is None


def test_segment_history_final_extras_is_none_for_em_suite(client):
    """em_suite_sampler does not publish extras (returns None); final_extras
    key is present but None in the single-model world."""
    resp = client.get("/api/segments/s1/history")
    assert resp.status_code == 200
    curves = resp.json()["estimator_curves"]
    assert "em_suite_sampler" in curves
    # EmSuiteSamplerEstimator.model_output always returns extras=None in Plan 1.
    assert curves["em_suite_sampler"]["final_extras"] is None


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

        from tests.conftest import make_test_config

        from spinlab.dashboard import create_app
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


# -- Removed endpoints return 404/405 ----------------------------------------

class TestRemovedEndpoints:
    def test_post_estimator_returns_405(self, client):
        """/api/estimator was the estimator-switch endpoint; it is gone."""
        resp = client.post("/api/estimator", json={"name": "kalman"})
        assert resp.status_code in (404, 405)

    def test_get_estimator_params_returns_404(self, client):
        """/api/estimator-params GET is gone."""
        resp = client.get("/api/estimator-params")
        assert resp.status_code in (404, 405)

    def test_post_estimator_params_returns_404(self, client):
        """/api/estimator-params POST is gone."""
        resp = client.post("/api/estimator-params", json={"params": {}})
        assert resp.status_code in (404, 405)
