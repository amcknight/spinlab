"""Tests for /api/segments/{id}/em-suite-matrix."""
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


def test_returns_404_for_unknown_segment(client):
    resp = client.get("/api/segments/does-not-exist/em-suite-matrix")
    assert resp.status_code == 404


def test_returns_empty_matrix_for_segment_with_no_events(db, client):
    seg_id = "seg-empty"
    db.upsert_segment(_make_segment(seg_id, level=1))
    resp = client.get(f"/api/segments/{seg_id}/em-suite-matrix")
    assert resp.status_code == 200
    body = resp.json()
    assert body["segment_id"] == seg_id
    assert body["n_attempts_total"] == 0
    assert body["n_successes"] == 0
    assert body["n_deaths"] == 0
    assert all(v is None for v in body["baseline"])
    for row in body["matrix"]:
        assert all(v is None for v in row)


def test_returns_populated_matrix_after_enough_events(db, client):
    seg_id = "seg-populated"
    db.upsert_segment(_make_segment(seg_id, level=2))
    # 2 successes + 2 deaths → gate passes for most alpha pairs
    events = [
        make_event_attempt(
            segment_id=seg_id, episode_id=f"e{i}",
            outcome=outcome, time_ms=time_ms,
        )
        for i, (outcome, time_ms) in enumerate([
            (AttemptOutcome.SURVIVED, 20_000),
            (AttemptOutcome.SURVIVED, 21_000),
            (AttemptOutcome.DIED, 5_000),
            (AttemptOutcome.DIED, 4_500),
        ])
    ]
    for ev in events:
        db.log_event_attempt(ev)
    resp = client.get(f"/api/segments/{seg_id}/em-suite-matrix")
    assert resp.status_code == 200
    body = resp.json()
    assert body["n_attempts_total"] == 4
    assert body["n_successes"] == 2
    assert body["n_deaths"] == 2
    assert len(body["alpha_grid"]) == 10
    assert len(body["baseline"]) == 10
    assert len(body["matrix"]) == 10
    # At least one non-None cell in the upper triangle
    assert any(
        v is not None
        for row in body["matrix"]
        for v in row
    )
    # param_history: three keys, each [10 alphas][n_events + 1 snapshots]
    assert set(body["param_history"].keys()) == {
        "p_die", "log_success_time", "log_death_time",
    }
    for key, grid in body["param_history"].items():
        assert len(grid) == 10, f"{key} alpha rows"
        assert all(len(row) == 5 for row in grid), f"{key} snapshot length"
        # α=0.0 anchor: snapshot 0 (empty state) is None; after observations
        # it accumulates the uniform all-time mean, so the row is NOT all-None.
        assert grid[0][0] is None, f"{key}: α=0 snapshot-0 should be None"
        assert any(v is not None for v in grid[0][1:]), (
            f"{key}: α=0 row should have non-None values after events"
        )
    # slope_matrices: three 10x10 upper-triangular grids
    assert set(body["slope_matrices"].keys()) == {
        "slope_log_success", "slope_log_death", "slope_logit_p",
    }
    for key, grid in body["slope_matrices"].items():
        assert len(grid) == 10
        assert all(len(row) == 10 for row in grid)
        # diagonal + lower triangle are None
        for fast_idx in range(10):
            for slow_idx in range(fast_idx, 10):
                assert grid[fast_idx][slow_idx] is None
