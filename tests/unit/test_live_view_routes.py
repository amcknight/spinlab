"""Route tests for the live-view endpoints."""
from __future__ import annotations

from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from spinlab.db import Database
from spinlab.estimators.session_snapshot import RouteBaseline, SessionSnapshot
from spinlab.models import AttemptOutcome, AttemptSource, EventAttempt, Segment
from spinlab.routes._deps import get_db, get_session
from spinlab.routes.model import router


class _NoSessionStub:
    """Stand-in SessionManager for route tests that don't care about session
    state. `practice_session_snapshot=None` so diff fields fall through to
    None (matches the "no active practice session" contract)."""
    practice_session_snapshot = None


class _ActiveSessionStub:
    """SessionManager stand-in with an active practice snapshot."""
    def __init__(self, snapshot: SessionSnapshot) -> None:
        self.practice_session_snapshot = snapshot


def _seed_db(tmp_path) -> tuple[Database, str, str]:
    db = Database(str(tmp_path / "t.db"))
    db.upsert_game("g1", "G", "any%")
    # live-summary is run-scoped: it aggregates only segments the active
    # reference run traversed. Activate a run and make the seeded segment a
    # member (one non-invalidated reference traversal row).
    ref_id = "g1:ref"
    db.create_capture_run(ref_id, "g1", "Ref", kind="live")
    db.promote_draft(ref_id, "Ref")
    db.set_active_capture_run(ref_id)
    seg_id = "g1:6:entrance.0:checkpoint.1:aa:bb"
    db.upsert_segment(Segment(
        id=seg_id, game_id="g1", level_number=6,
        start_type="entrance", start_ordinal=0,
        end_type="checkpoint", end_ordinal=1, active=True))
    db.create_session("g1:s", "g1")
    db.log_event_attempt(EventAttempt(
        segment_id=seg_id, episode_id="reftrav",
        outcome=AttemptOutcome.SURVIVED, time_ms=4000,
        capture_run_id=ref_id, source=AttemptSource.REFERENCE,
        created_at=datetime.now(UTC)))
    for i in range(8):
        for outcome, t in ((AttemptOutcome.DIED, 1500), (AttemptOutcome.SURVIVED, 4200 - i * 20)):
            db.log_event_attempt(EventAttempt(
                segment_id=seg_id, session_id="g1:s", episode_id=f"{outcome.value}{i}",
                outcome=outcome, time_ms=t, source=AttemptSource.PRACTICE,
                created_at=datetime.now(UTC)))
    return db, seg_id, "g1"


def _client(tmp_path) -> tuple[TestClient, str, str]:
    db, seg_id, game_id = _seed_db(tmp_path)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_session] = lambda: _NoSessionStub()
    return TestClient(app), seg_id, game_id


def _client_with_session(tmp_path) -> tuple[TestClient, str, str]:
    db, seg_id, game_id = _seed_db(tmp_path)
    # started_at=0.0 (1970 UTC) is before every just-logged event, so all events
    # count as in-session and route_series produces points. Also exercises the
    # tz-aware comparison: a naive session_start here would raise.
    snapshot = SessionSnapshot(
        started_at=0.0,
        segments={},
        route=RouteBaseline(exp_run_ms=250000.0, exp_deaths=20.0),
    )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_session] = lambda: _ActiveSessionStub(snapshot)
    return TestClient(app), seg_id, game_id


def _client_with_frozen_session(tmp_path) -> tuple[TestClient, str, str]:
    db, seg_id, game_id = _seed_db(tmp_path)
    snapshot = SessionSnapshot(
        started_at=0.0, ended_at=60.0, segments={},
        route=RouteBaseline(exp_run_ms=250000.0, exp_deaths=20.0),
    )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_session] = lambda: _ActiveSessionStub(snapshot)
    return TestClient(app), seg_id, game_id


class TestLiveRoutes:
    def test_segment_live_ready(self, tmp_path):
        client, seg_id, _ = _client(tmp_path)
        r = client.get(f"/api/segments/{seg_id}/live")
        assert r.status_code == 200
        b = r.json()
        assert b["segment_id"] == seg_id
        assert b["ready"] is True
        assert b["expected_episode_ms"] is not None
        assert isinstance(b["series"], list) and len(b["series"]) >= 1

    def test_segment_live_unknown_404(self, tmp_path):
        client, _, _ = _client(tmp_path)
        r = client.get("/api/segments/nope/live")
        assert r.status_code == 404

    def test_route_summary(self, tmp_path):
        client, _, game_id = _client(tmp_path)
        r = client.get(f"/api/games/{game_id}/live-summary")
        assert r.status_code == 200
        b = r.json()
        assert b["game_id"] == game_id
        assert b["n_estimable"] + b["n_skipped"] >= 1

    def test_live_summary_includes_run_series_fields(self, tmp_path):
        client, _, game_id = _client_with_session(tmp_path)
        r = client.get(f"/api/games/{game_id}/live-summary")
        assert r.status_code == 200
        body = r.json()
        assert "run_series" in body and isinstance(body["run_series"], list)
        assert "baseline_exp_run_ms" in body
        assert "floor_total_ms" in body
        assert "floor_series" in body and isinstance(body["floor_series"], list)
        # Active session with in-session events -> the series should populate.
        assert len(body["run_series"]) >= 1
        # floor_series is aligned point-for-point with run_series.
        assert len(body["floor_series"]) == len(body["run_series"])
        # baseline echoes the snapshot route; no baseline segments -> floor_improvement 0.
        assert body["baseline_exp_run_ms"] == 250000.0
        assert body["floor_improvement_ms"] == 0.0


class TestFrozenLiveSummary:
    def test_live_summary_exposes_session_ended_at_when_frozen(self, tmp_path):
        client, _, game_id = _client_with_frozen_session(tmp_path)
        r = client.get(f"/api/games/{game_id}/live-summary")
        assert r.status_code == 200
        assert r.json()["session_ended_at"] == 60.0

    def test_live_summary_session_ended_at_none_when_live(self, tmp_path):
        # _client_with_session (Plan 1) builds a snapshot with ended_at unset.
        client, _, game_id = _client_with_session(tmp_path)
        r = client.get(f"/api/games/{game_id}/live-summary")
        assert r.status_code == 200
        assert r.json()["session_ended_at"] is None
