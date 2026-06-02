"""Route test for /api/segments/{id}/progress."""
from __future__ import annotations

from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from spinlab.db import Database
from spinlab.estimators.em_suite_sampler import SamplerState, process_event
from spinlab.models import AttemptOutcome, AttemptSource, EventAttempt, Segment
from spinlab.routes._deps import get_db
from spinlab.routes.model import router


def _client(tmp_path) -> tuple[TestClient, str]:
    db = Database(str(tmp_path / "t.db"))
    db.upsert_game("g1", "G", "any%")
    seg_id = "g1:6:entrance.0:checkpoint.1:aa:bb"
    db.upsert_segment(Segment(
        id=seg_id, game_id="g1", level_number=6,
        start_type="entrance", start_ordinal=0,
        end_type="checkpoint", end_ordinal=1, active=True))
    sess = "g1:s"
    db.create_session(sess, "g1")
    state = SamplerState()
    for i in range(8):
        for outcome, t in ((AttemptOutcome.DIED, 1500), (AttemptOutcome.SURVIVED, 4200 - i * 20)):
            ev = EventAttempt(
                segment_id=seg_id, session_id=sess, episode_id=f"{outcome.value}{i}",
                outcome=outcome, time_ms=t, source=AttemptSource.PRACTICE,
                created_at=datetime.now(UTC))
            db.log_event_attempt(ev)
            state = process_event(state, ev)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app), seg_id


class TestProgressRoute:
    def test_gated_segment_returns_ready_payload(self, tmp_path):
        client, seg_id = _client(tmp_path)
        resp = client.get(f"/api/segments/{seg_id}/progress")
        assert resp.status_code == 200
        data = resp.json()
        assert data["segment_id"] == seg_id
        assert data["ready"] is True
        assert data["verdict"] in ("faster", "holding", "slower")
        assert data["now_clear_ms"] is not None
        assert len(data["trend_ms"]) >= 1

    def test_unknown_segment_404(self, tmp_path):
        client, _ = _client(tmp_path)
        resp = client.get("/api/segments/does-not-exist/progress")
        assert resp.status_code == 404
