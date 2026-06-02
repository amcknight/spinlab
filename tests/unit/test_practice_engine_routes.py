"""Tests for /api/practice-engine routes."""
from __future__ import annotations

import json
import random
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from spinlab.dashboard import create_app
from spinlab.db import Database
from spinlab.estimators.em_suite_sampler import SamplerState, process_event
from spinlab.models import (
    AttemptOutcome,
    AttemptSource,
    EventAttempt,
    Segment,
)


def _seed_gated_state(db, segment_id, n_events=60):
    state = SamplerState(n_completed=0, n_attempts=0)
    rng = random.Random(segment_id)
    if db.get_current_session("g1") is None:
        db.create_session("sess_pe", "g1")
    for i in range(n_events):
        outcome = AttemptOutcome.SURVIVED if i % 3 != 0 else AttemptOutcome.DIED
        t_ms = 4000 + rng.randint(-200, 200) if outcome == AttemptOutcome.SURVIVED else 1500
        ev = EventAttempt(
            segment_id=segment_id, session_id="sess_pe",
            episode_id=f"{segment_id}_e{i}",
            outcome=outcome, time_ms=t_ms,
            source=AttemptSource.PRACTICE,
            created_at=datetime.now(UTC),
        )
        db.log_event_attempt(ev)
        state = process_event(state, ev)
    db.save_model_state(
        segment_id, "em_suite_sampler",
        json.dumps(state.to_dict()),
        json.dumps({"total": {"expected_ms": None, "ms_per_attempt": None, "floor_ms": None},
                    "clean": {"expected_ms": None, "ms_per_attempt": None, "floor_ms": None}}),
    )


@pytest.fixture
def client_with_gated(tmp_path):
    from tests.conftest import make_test_config
    db = Database(str(tmp_path / "test.db"))
    db.upsert_game("g1", "Game", "any%")
    for seg_id, level in [("s1", 1), ("s2", 2)]:
        seg = Segment(
            id=seg_id, game_id="g1", level_number=level,
            start_type="entrance", start_ordinal=0,
            end_type="goal", end_ordinal=0,
            description=f"Level {level}",
        )
        db.upsert_segment(seg)
        _seed_gated_state(db, seg_id)
    app = create_app(db=db, config=make_test_config())
    app.state.session.game_id = "g1"
    app.state.session.game_name = "Game"
    return TestClient(app)


@pytest.fixture
def client_no_game(tmp_path):
    from tests.conftest import make_test_config
    db = Database(str(tmp_path / "test.db"))
    db.upsert_game("g1", "Game", "any%")
    app = create_app(db=db, config=make_test_config())
    return TestClient(app)


class TestStateEndpoint:
    def test_returns_gated_segments(self, client_with_gated):
        resp = client_with_gated.get("/api/practice-engine/state")
        assert resp.status_code == 200
        data = resp.json()
        assert {s["seg_id"] for s in data["gated_segments"]} == {"s1", "s2"}
        for s in data["gated_segments"]:
            assert s["e_sample_0_ms"] > 0
            assert s["pool_success"] > 0
            assert s["pool_death"] > 0

    def test_no_game_returns_empty(self, client_no_game):
        resp = client_no_game.get("/api/practice-engine/state")
        assert resp.status_code == 200
        data = resp.json()
        assert data["gated_segments"] == []
        assert data["ungated_segments"] == []


class TestEvaluateEndpoint:
    def test_no_reset_expected_wall_clock(self, client_with_gated):
        resp = client_with_gated.post(
            "/api/practice-engine/evaluate",
            json={
                "policy": "no_reset", "policy_kwargs": {},
                "objective": "expected_wall_clock_per_attempt", "objective_ctx": {},
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["objective_value"] is not None
        assert data["total_time_summary"]["finished_pct"] == pytest.approx(100.0)
        assert len(data["per_segment_values"]) == 2

    def test_target_paced_with_thresholds(self, client_with_gated):
        resp = client_with_gated.post(
            "/api/practice-engine/evaluate",
            json={
                "policy": "target_paced",
                "policy_kwargs": {
                    "cum_splits_ms": {"s1": 6000, "s2": 12000},
                    "slack": 0.1,
                },
                "objective": "expected_wall_clock_per_attempt", "objective_ctx": {},
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert 0.0 <= data["total_time_summary"]["finished_pct"] <= 100.0

    def test_q_objective_requires_target_ms(self, client_with_gated):
        resp = client_with_gated.post(
            "/api/practice-engine/evaluate",
            json={
                "policy": "no_reset", "policy_kwargs": {},
                "objective": "q", "objective_ctx": {"target_ms": 7000},
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["objective_value"] is not None
        assert 0.0 <= data["objective_value"] <= 1.0

    def test_q_objective_without_target_ms_422(self, client_with_gated):
        # Missing required ctx must 422 at the boundary, not KeyError-500 inside
        # the objective. Regression: selecting q with a blank target crashed eval.
        resp = client_with_gated.post(
            "/api/practice-engine/evaluate",
            json={
                "policy": "no_reset", "policy_kwargs": {},
                "objective": "q", "objective_ctx": {},
            },
        )
        assert resp.status_code == 422
        assert "target_ms" in resp.json()["detail"]

    def test_p_pb_requires_both_ctx_keys_422(self, client_with_gated):
        resp = client_with_gated.post(
            "/api/practice-engine/evaluate",
            json={
                "policy": "no_reset", "policy_kwargs": {},
                "objective": "p_pb_this_session", "objective_ctx": {"target_ms": 7000},
            },
        )
        assert resp.status_code == 422
        assert "session_remaining_ms" in resp.json()["detail"]

    def test_unknown_objective_400(self, client_with_gated):
        resp = client_with_gated.post(
            "/api/practice-engine/evaluate",
            json={
                "policy": "no_reset", "policy_kwargs": {},
                "objective": "not_a_real_objective", "objective_ctx": {},
            },
        )
        assert resp.status_code == 422  # Pydantic Literal mismatch
