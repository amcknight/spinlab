"""Route tests for the live-view endpoints."""
from __future__ import annotations

from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from spinlab.db import Database
from spinlab.models import AttemptOutcome, AttemptSource, EventAttempt, Segment
from spinlab.routes.model import router
from spinlab.routes._deps import get_db


def _client(tmp_path) -> tuple[TestClient, str, str]:
    db = Database(str(tmp_path / "t.db"))
    db.upsert_game("g1", "G", "any%")
    seg_id = "g1:6:entrance.0:checkpoint.1:aa:bb"
    db.upsert_segment(Segment(
        id=seg_id, game_id="g1", level_number=6,
        start_type="entrance", start_ordinal=0,
        end_type="checkpoint", end_ordinal=1, active=True))
    db.create_session("g1:s", "g1")
    for i in range(8):
        for outcome, t in ((AttemptOutcome.DIED, 1500), (AttemptOutcome.SURVIVED, 4200 - i * 20)):
            db.log_event_attempt(EventAttempt(
                segment_id=seg_id, session_id="g1:s", episode_id=f"{outcome.value}{i}",
                outcome=outcome, time_ms=t, source=AttemptSource.PRACTICE,
                created_at=datetime.now(UTC)))
    app = FastAPI(); app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app), seg_id, "g1"


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
