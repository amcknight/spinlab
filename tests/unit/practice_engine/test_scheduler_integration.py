"""Tests for Scheduler ↔ PracticeEngine integration."""
from __future__ import annotations

import json
import random
from datetime import UTC, datetime

from spinlab.db import Database
from spinlab.estimators.em_suite_sampler import SamplerState, process_event
from spinlab.models import (
    Attempt,
    AttemptOutcome,
    AttemptSource,
    EventAttempt,
    Segment,
)
from spinlab.scheduler import Scheduler


def _seed_gated_state(db: Database, segment_id: str, n_events: int = 60) -> None:
    """Insert enough events to gate the segment's SamplerState."""
    state = SamplerState(n_completed=0, n_attempts=0)
    rng = random.Random(segment_id)
    # Use a stable session_id keyed by segment so the seeding doesn't collide.
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


def _seeded_db(tmp_path) -> Database:
    db = Database(str(tmp_path / "test.db"))
    db.upsert_game("g1", "Game", "any%")
    for seg_id, level in [("s1", 1), ("s2", 2)]:
        seg = Segment(
            id=seg_id, game_id="g1", level_number=level,
            start_type="entrance", start_ordinal=0,
            end_type="goal", end_ordinal=0,
        )
        db.upsert_segment(seg)
        _seed_gated_state(db, seg_id)
    return db


class TestSchedulerEngineLazy:
    def test_engine_built_on_first_access(self, tmp_path):
        db = _seeded_db(tmp_path)
        sched = Scheduler(db, "g1")
        engine = sched.engine
        engine.matrix.ensure_fresh()
        assert "s1" in engine.matrix.seg_ids
        assert "s2" in engine.matrix.seg_ids

    def test_engine_invalidation_on_attempt_completion(self, tmp_path):
        db = _seeded_db(tmp_path)
        sched = Scheduler(db, "g1")
        engine = sched.engine
        engine.matrix.ensure_fresh()
        assert engine.matrix.dirty == set()
        # Simulate an attempt landing:
        db.log_attempt(Attempt(
            segment_id="s1", session_id="sess_pe",
            completed=True, time_ms=5000,
        ))
        sched.update_state_after_episode("s1")
        # Engine column for s1 should now be dirty:
        assert "s1" in engine.matrix.dirty
        assert "s2" not in engine.matrix.dirty


class TestSchedulerLoadAllSamplerStates:
    def test_returns_gated_states_only(self, tmp_path):
        db = _seeded_db(tmp_path)
        # Add a third segment that has NO events (no model_state row):
        db.upsert_segment(Segment(
            id="s3", game_id="g1", level_number=3,
            start_type="entrance", start_ordinal=0,
            end_type="goal", end_ordinal=0,
        ))
        sched = Scheduler(db, "g1")
        states = sched._load_all_sampler_states()
        # s1 and s2 have model_state rows; s3 doesn't → not present
        assert set(states.keys()) == {"s1", "s2"}
