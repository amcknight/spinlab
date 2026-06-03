"""GET /api/segments/{id}/live and /api/games/{id}/live-summary — session-diff fields.

When no practice session is active, all *_diff_* and practice_saved_ms /
session_started_at / floor_improvement_ms fields must be None. (When a session
is active, the live-view payload's *_diff_* fields populate; the reducer-level
formulas are covered in tests/unit/estimators/test_live_view.py — these route
tests only verify the no-session default contract.)
"""
from __future__ import annotations

from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from spinlab.db import Database
from spinlab.models import AttemptOutcome, AttemptSource, EventAttempt, Segment
from spinlab.routes._deps import get_db, get_session
from spinlab.routes.model import router


class _NoSessionStub:
    """Stand-in SessionManager with no active practice session. The routes
    only touch `practice_session_snapshot`; we don't need the full SM."""
    practice_session_snapshot = None


def _client(tmp_path) -> tuple[TestClient, str, str]:
    """Seed a game + one segment with enough completed episodes for the live
    view to gate ready=True. Mirrors the pattern in test_live_view_routes.py
    and test_segment_progress_route.py."""
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
    app.dependency_overrides[get_session] = lambda: _NoSessionStub()
    return TestClient(app), seg_id, "g1"


def test_segments_live_diffs_null_without_active_session(tmp_path):
    """No practice session => all four *_diff_* fields are None, but the
    base reducer fields (expected_episode_ms, floor_ms, ...) still populate."""
    client, seg_id, _ = _client(tmp_path)
    r = client.get(f"/api/segments/{seg_id}/live")
    assert r.status_code == 200
    body = r.json()
    # Sanity: payload populated as usual when not in session.
    assert body["segment_id"] == seg_id
    assert body["ready"] is True
    # Contract under test: session diffs all None.
    assert body["expected_episode_diff_ms"] is None
    assert body["practice_gain_diff_ms"] is None
    assert body["floor_diff_ms"] is None
    assert body["death_rate_diff"] is None


def test_route_summary_session_fields_null_without_active_session(tmp_path):
    """No practice session => session_started_at, all *_diff_* fields, plus
    practice_saved_ms and floor_improvement_ms are all None."""
    client, _, game_id = _client(tmp_path)
    r = client.get(f"/api/games/{game_id}/live-summary")
    assert r.status_code == 200
    body = r.json()
    # Sanity: base aggregate fields still present.
    assert body["game_id"] == game_id
    # Contract under test: all session-overlay fields None.
    assert body["session_started_at"] is None
    assert body["exp_run_diff_ms"] is None
    assert body["exp_deaths_diff"] is None
    assert body["practice_saved_ms"] is None
    assert body["floor_improvement_ms"] is None
