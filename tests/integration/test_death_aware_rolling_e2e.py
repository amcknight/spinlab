"""End-to-end pipeline test for the death-aware rolling estimator.

Drives several multi-death practice episodes through the real DB +
scheduler + estimator stack and asserts the persisted output is
populated with sensible values.

No emulator — uses the DB directly. Events are written via
log_event_attempt the same way PracticeSession.receive_event_attempt does.
"""
import json
from datetime import datetime

import pytest


def _log_episode(db, segment_id, session_id, episode_id, outcomes_and_times):
    """Persist one episode as a series of event rows. Returns the closing id."""
    from spinlab.models import (
        AttemptOutcome, AttemptSource, EventAttempt,
    )
    last_id = None
    for outcome, time_ms in outcomes_and_times:
        last_id = db.log_event_attempt(EventAttempt(
            segment_id=segment_id,
            episode_id=episode_id,
            outcome=AttemptOutcome(outcome),
            time_ms=time_ms,
            session_id=session_id,
            capture_run_id=None,
            source=AttemptSource.PRACTICE,
            chosen_allocator=None,
            invalidated=False,
            created_at=datetime.now(),
        ))
    return last_id


class TestDeathAwareE2E:
    def test_multi_death_flow_persists_sensible_outputs(self, tmp_path):
        from spinlab.db import Database
        from spinlab.models import EndpointType, Segment
        from spinlab.scheduler import Scheduler

        # Database.__init__ calls run_migrations automatically.
        db = Database(tmp_path / "spinlab.db")
        game_id = "test_game"
        segment_id = "seg_e2e"
        session_id = f"{game_id}:sess"
        db.upsert_game(game_id, "FakeGame", "any%")
        db.upsert_segment(Segment(
            id=segment_id, game_id=game_id, level_number=1,
            start_type=EndpointType.ENTRANCE, start_ordinal=0,
            end_type=EndpointType.GOAL, end_ordinal=0,
            description="e2e segment",
        ))
        db.create_session(session_id, game_id)

        # Scheduler constructor: Scheduler(db, game_id, estimator_name=...)
        sched = Scheduler(db, game_id, estimator_name="death_aware_rolling")

        # Episode 1: died at 3000, then survived at 8000.
        _log_episode(db, segment_id, session_id, "ep1", [
            ("died", 3000), ("survived", 8000),
        ])
        sched.update_state_after_episode(segment_id)

        # Episode 2: clean survival at 7500.
        _log_episode(db, segment_id, session_id, "ep2", [
            ("survived", 7500),
        ])
        sched.update_state_after_episode(segment_id)

        # Episode 3: aborted after three deaths.
        _log_episode(db, segment_id, session_id, "ep3", [
            ("died", 2500), ("died", 2800), ("died", 3100),
        ])
        sched.update_state_after_episode(segment_id)

        # Episode 4: died once then survived.
        _log_episode(db, segment_id, session_id, "ep4", [
            ("died", 2200), ("survived", 8200),
        ])
        sched.update_state_after_episode(segment_id)

        # Inspect persisted state.
        row = db.load_model_state(segment_id, "death_aware_rolling")
        assert row is not None
        output = json.loads(row["output_json"])

        # Total expected ms is populated (we have completions).
        assert output["total"]["expected_ms"] is not None
        assert output["total"]["expected_ms"] > 0
        assert output["clean"]["expected_ms"] is not None

        # Floors reflect the best episode totals / completion times.
        assert output["total"]["floor_ms"] is not None
        assert output["clean"]["floor_ms"] is not None
        # Best clean survived time in this dataset is 7500 (ep2).
        assert output["clean"]["floor_ms"] == pytest.approx(7500.0)

        # Extras populated.
        extras = output["extras"]
        assert extras is not None
        assert 0.0 < extras["p_die_per_life"] < 1.0

        # n_attempts_effective is the sum of decayed weights over 4 episodes.
        # With default halflife=20: weights ≈ [0.901, 0.933, 0.966, 1.0],
        # summing to ~3.80 — not integer-count 4.0.
        assert 3.5 < extras["n_attempts_effective"] < 4.0

        # Total died events: ep1(1) + ep3(3) + ep4(1) = 5.
        assert len(extras["death_samples"]) == 5
        # Total survived events: ep1(1) + ep2(1) + ep4(1) = 3.
        assert len(extras["completion_samples"]) == 3

    def test_invalidated_episode_excluded_e2e(self, tmp_path):
        from spinlab.db import Database
        from spinlab.models import EndpointType, Segment
        from spinlab.scheduler import Scheduler

        # Database.__init__ calls run_migrations automatically.
        db = Database(tmp_path / "spinlab.db")
        game_id = "test_game"
        segment_id = "seg_inv"
        session_id = f"{game_id}:sess"
        db.upsert_game(game_id, "FakeGame", "any%")
        db.upsert_segment(Segment(
            id=segment_id, game_id=game_id, level_number=1,
            start_type=EndpointType.ENTRANCE, start_ordinal=0,
            end_type=EndpointType.GOAL, end_ordinal=0,
            description="invalidation test",
        ))
        db.create_session(session_id, game_id)

        sched = Scheduler(db, game_id, estimator_name="death_aware_rolling")

        last_id_bad = _log_episode(db, segment_id, session_id, "ep_bad", [
            ("died", 100), ("died", 200), ("died", 300),
        ])
        # Mark one event in the bad episode invalidated.
        # _group_into_episodes drops the whole episode if ANY event is invalidated.
        db.set_attempt_invalidated(last_id_bad, True)

        _log_episode(db, segment_id, session_id, "ep_good", [
            ("survived", 8000),
        ])
        sched.update_state_after_episode(segment_id)

        row = db.load_model_state(segment_id, "death_aware_rolling")
        output = json.loads(row["output_json"])
        extras = output["extras"]
        # Invalidated episode does not contribute — only the clean one counts.
        assert extras["p_die_per_life"] == pytest.approx(0.0)
        assert len(extras["death_samples"]) == 0
        assert len(extras["completion_samples"]) == 1
