"""Tests for the cold_distribution field on /api/segments/{id}/history."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from tests.conftest import make_test_config
from tests.factories import make_event_attempt

from spinlab.dashboard import create_app
from spinlab.db import Database
from spinlab.models import AttemptOutcome, Segment


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    d.upsert_game("g1", "Test Game", "any%")
    # make_event_attempt defaults session_id="_default_test_session"; the FK
    # must exist before log_event_attempt can insert.
    d.create_session("_default_test_session", "g1")
    return d


@pytest.fixture
def client(db):
    app = create_app(db=db, config=make_test_config())
    app.state.session.game_id = "g1"
    app.state.session.game_name = "Test Game"
    return TestClient(app)


def _make_segment(segment_id: str, level: int) -> Segment:
    return Segment(
        id=segment_id, game_id="g1", level_number=level,
        start_type="entrance", start_ordinal=0,
        end_type="goal", end_ordinal=0,
    )


def test_segment_history_returns_cold_distribution(db, client):
    """Segment with cold + hot attempts → cold_distribution computed from cold subset only."""
    seg_id = "seg-test-1"
    db.upsert_segment(_make_segment(seg_id, level=1))
    # 2 cold events (die at 1500, survive at 3000), 1 hot event (die at 9999)
    for ev in [
        make_event_attempt(
            segment_id=seg_id, episode_id="e1",
            outcome=AttemptOutcome.DIED, time_ms=1500, is_hot=False,
        ),
        make_event_attempt(
            segment_id=seg_id, episode_id="e1",
            outcome=AttemptOutcome.SURVIVED, time_ms=3000, is_hot=False,
        ),
        make_event_attempt(
            segment_id=seg_id, episode_id="e2",
            outcome=AttemptOutcome.DIED, time_ms=9999, is_hot=True,
        ),
    ]:
        db.log_event_attempt(ev)

    resp = client.get(f"/api/segments/{seg_id}/history")
    assert resp.status_code == 200
    data = resp.json()
    assert data["cold_distribution"] is not None
    cd = data["cold_distribution"]
    # 2 cold attempts, NOT 3 (hot dropped)
    assert cd["n_cold_attempts"] == 2
    total_deaths = sum(b["n_deaths"] for b in cd["bins"])
    total_completions = sum(b["n_completions"] for b in cd["bins"])
    assert total_deaths == 1
    assert total_completions == 1
    # hi from cold max (3000), NOT from hot max (9999)
    assert cd["bins"][-1]["hi_ms"] == 3000
    # Hazard math flows end-to-end.
    # With 2 cold events (death @1500, survival @3000), the range spans all bins,
    # so every bin has at_risk_w > 0 and a real hazard value (never None).
    assert all(b["at_risk_w"] > 0 for b in cd["bins"])
    assert all(b["hazard"] is not None for b in cd["bins"])
    # The bin containing the death must have hazard > 0; all others are 0.0.
    death_bins = [b for b in cd["bins"] if b["n_deaths"] > 0]
    assert len(death_bins) == 1
    assert death_bins[0]["hazard"] > 0
    no_death_bins = [b for b in cd["bins"] if b["n_deaths"] == 0]
    assert all(b["hazard"] == 0.0 for b in no_death_bins)


def test_segment_history_returns_null_when_all_hot(db, client):
    seg_id = "seg-test-2"
    db.upsert_segment(_make_segment(seg_id, level=2))
    db.log_event_attempt(make_event_attempt(
        segment_id=seg_id, episode_id="e1",
        outcome=AttemptOutcome.DIED, time_ms=1000, is_hot=True,
    ))
    resp = client.get(f"/api/segments/{seg_id}/history")
    assert resp.status_code == 200
    assert resp.json()["cold_distribution"] is None


def test_segment_history_returns_null_when_no_events(db, client):
    seg_id = "seg-test-3"
    db.upsert_segment(_make_segment(seg_id, level=3))
    resp = client.get(f"/api/segments/{seg_id}/history")
    assert resp.status_code == 200
    assert resp.json()["cold_distribution"] is None
