"""Run-scoping of model-view read paths (reference-run-selector Task 2).

A "reference run" scopes WHICH segments are visible on every model view.
Membership = the segment has >=1 non-invalidated row in the ``attempts`` table
stamped with the active run's ``capture_run_id`` (it was traversed). With no
active run, views return an EMPTY set (not the whole game).

These tests pin the contract for:
- ``/api/model`` (scheduler ``get_all_model_states`` chokepoint)
- ``/api/games/{id}/live-summary`` aggregate
- ``/api/practice-engine/state`` (simulator)
- ``/api/segments`` (segments list)
- the scheduler practice universe (``get_all_model_states`` / ``pick_next``)

and the pooling INVARIANT: ``get_segment_attempts`` stays game-wide regardless
of which run is active.
"""
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
from tests.factories import stamp_reference_traversal

GAME_ID = "g1"


def _seed_gated_state(db: Database, segment_id: str, run_id: str | None,
                      n_events: int = 60) -> None:
    """Seed a gated em_suite_sampler model_state for ``segment_id``.

    Events are PRACTICE (geography-pooled). When ``run_id`` is given, also stamp
    one REFERENCE traversal row so the segment is a member of that run.
    """
    state = SamplerState(n_completed=0, n_attempts=0)
    rng = random.Random(segment_id)
    if db.get_current_session(GAME_ID) is None:
        db.create_session("sess_pe", GAME_ID)
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
    if run_id is not None:
        stamp_reference_traversal(db, segment_id, run_id)


def _make_segment(db: Database, seg_id: str, level: int) -> None:
    db.upsert_segment(Segment(
        id=seg_id, game_id=GAME_ID, level_number=level,
        start_type="entrance", start_ordinal=0,
        end_type="goal", end_ordinal=0,
        description=f"Level {level}", active=True,
    ))


def _client(db: Database) -> TestClient:
    from tests.conftest import make_test_config
    app = create_app(db=db, config=make_test_config())
    app.state.session.game_id = GAME_ID
    app.state.session.game_name = "Game"
    return TestClient(app)


@pytest.fixture
def db(tmp_path) -> Database:
    d = Database(str(tmp_path / "test.db"))
    d.upsert_game(GAME_ID, "Game", "any%")
    return d


def _open_run(db: Database, run_id: str) -> None:
    db.create_capture_run(run_id, GAME_ID, run_id, kind="live")
    db.promote_draft(run_id, run_id)


class TestActiveRunScopesToTraversedSegments:
    """Active run traversed only X (of two active segments) -> views show only X."""

    def _setup_two_segments_run_sees_one(self, db: Database) -> None:
        # Two active gated segments; run "R" traverses only s1.
        _open_run(db, "R")
        _make_segment(db, "s1", 1)
        _make_segment(db, "s2", 2)
        _seed_gated_state(db, "s1", run_id="R")
        _seed_gated_state(db, "s2", run_id=None)  # NOT a member of R
        db.set_active_capture_run("R")

    def test_api_model_returns_only_traversed_segment(self, db):
        self._setup_two_segments_run_sees_one(db)
        client = _client(db)
        resp = client.get("/api/model")
        assert resp.status_code == 200
        seg_ids = {s["segment_id"] for s in resp.json()["segments"]}
        assert seg_ids == {"s1"}

    def test_scheduler_universe_contains_only_traversed_segment(self, db):
        self._setup_two_segments_run_sees_one(db)
        client = _client(db)
        sched = client.app.state.session.get_scheduler()
        states = sched.get_all_model_states()
        assert {s.segment_id for s in states} == {"s1"}

    def test_live_summary_aggregates_only_traversed_segment(self, db):
        self._setup_two_segments_run_sees_one(db)
        client = _client(db)
        resp = client.get(f"/api/games/{GAME_ID}/live-summary")
        assert resp.status_code == 200
        body = resp.json()
        # Exactly one segment is in scope -> exactly one estimable/skipped slot.
        assert body["n_estimable"] + body["n_skipped"] == 1

    def test_simulator_state_returns_only_traversed_segment(self, db):
        self._setup_two_segments_run_sees_one(db)
        client = _client(db)
        resp = client.get("/api/practice-engine/state")
        assert resp.status_code == 200
        data = resp.json()
        all_ids = {s["seg_id"] for s in data["gated_segments"]} | {
            s["seg_id"] for s in data["ungated_segments"]}
        assert all_ids == {"s1"}

    def test_segments_list_returns_only_traversed_segment(self, db):
        self._setup_two_segments_run_sees_one(db)
        client = _client(db)
        resp = client.get("/api/segments")
        assert resp.status_code == 200
        ids = {s["id"] for s in resp.json()["segments"]}
        assert ids == {"s1"}


class TestNoActiveRunReturnsEmpty:
    """No active run -> every model view is empty (NOT whole-game)."""

    def _setup_segments_no_active_run(self, db: Database) -> None:
        # Segments + model states exist, but no run is active.
        _make_segment(db, "s1", 1)
        _make_segment(db, "s2", 2)
        _seed_gated_state(db, "s1", run_id=None)
        _seed_gated_state(db, "s2", run_id=None)
        assert db.get_active_capture_run(GAME_ID) is None

    def test_api_model_empty(self, db):
        self._setup_segments_no_active_run(db)
        client = _client(db)
        resp = client.get("/api/model")
        assert resp.status_code == 200
        assert resp.json()["segments"] == []

    def test_scheduler_universe_empty(self, db):
        self._setup_segments_no_active_run(db)
        client = _client(db)
        sched = client.app.state.session.get_scheduler()
        assert sched.get_all_model_states() == []
        assert sched.pick_next() is None

    def test_live_summary_zero_aggregates(self, db):
        self._setup_segments_no_active_run(db)
        client = _client(db)
        resp = client.get(f"/api/games/{GAME_ID}/live-summary")
        assert resp.status_code == 200
        body = resp.json()
        assert body["n_estimable"] == 0
        assert body["n_skipped"] == 0

    def test_simulator_state_empty(self, db):
        self._setup_segments_no_active_run(db)
        client = _client(db)
        resp = client.get("/api/practice-engine/state")
        assert resp.status_code == 200
        data = resp.json()
        assert data["N"] == 0 or (
            data["gated_segments"] == [] and data["ungated_segments"] == [])

    def test_segments_list_empty(self, db):
        self._setup_segments_no_active_run(db)
        client = _client(db)
        resp = client.get("/api/segments")
        assert resp.status_code == 200
        assert resp.json()["segments"] == []


class TestPoolingInvariantUnchanged:
    """A segment shared by run A and run B keeps its full geography attempt
    history regardless of which run is active. get_segment_attempts is NOT
    scoped — pooling is by geography, not by run."""

    def test_get_segment_attempts_is_run_wide(self, db):
        _open_run(db, "A")
        _open_run(db, "B")
        _make_segment(db, "shared", 1)
        db.create_session("sess", GAME_ID)
        # Three completed reference episodes via run A (pooled by geography).
        for i in range(3):
            stamp_reference_traversal(db, "shared", "A", time_ms=2000,
                                      episode_suffix=f"a{i}")
        # Also traversed by B.
        stamp_reference_traversal(db, "shared", "B", time_ms=2100,
                                  episode_suffix="b0")

        # Pooling reads ALL non-invalidated attempts on the geography,
        # independent of which run is active.
        db.set_active_capture_run("A")
        attempts_under_a = db.get_segment_attempts("shared")
        db.set_active_capture_run("B")
        attempts_under_b = db.get_segment_attempts("shared")
        assert len(attempts_under_a) == len(attempts_under_b)
        # 3 (run A) + 1 (run B) reference traversals = 4 episodes, all pooled.
        assert len(attempts_under_a) == 4
