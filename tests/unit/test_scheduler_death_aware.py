"""Scheduler-level test: events flow through to the new estimator."""
import json

import pytest


def _seed_segment_with_events(db, segment_id: str, game_id: str):
    """Create a segment with one completed episode (1 death + 1 survival)."""
    from spinlab.models import (
        AttemptOutcome,
        AttemptSource,
        EndpointType,
        EventAttempt,
        Segment,
    )
    db.upsert_game(game_id, "FakeGame", "any%")
    db.upsert_segment(Segment(
        id=segment_id, game_id=game_id, level_number=1,
        start_type=EndpointType.ENTRANCE, start_ordinal=0,
        end_type=EndpointType.GOAL, end_ordinal=0,
        description="seg1",
    ))
    session_id = f"{game_id}:sess"
    db.create_session(session_id, game_id)
    # One episode: died at 3000ms, then survived at 8000ms.
    from datetime import datetime
    episode_id = "ep1"
    common = dict(
        segment_id=segment_id, episode_id=episode_id,
        session_id=session_id, capture_run_id=None,
        source=AttemptSource.PRACTICE,
        chosen_allocator=None, invalidated=False,
        created_at=datetime.fromisoformat("2026-05-24T00:00:00"),
    )
    db.log_event_attempt(EventAttempt(outcome=AttemptOutcome.DIED, time_ms=3000, **common))
    db.log_event_attempt(EventAttempt(outcome=AttemptOutcome.SURVIVED, time_ms=8000, **common))
    return session_id


class TestEventsFromRows:
    def test_is_hot_round_trips_through_events_from_rows(self):
        """_events_from_rows must hydrate is_hot from the DB row (not default False)."""
        from datetime import datetime

        from spinlab.db import Database
        from spinlab.models import (
            AttemptOutcome,
            AttemptSource,
            EndpointType,
            EventAttempt,
            Segment,
        )
        from spinlab.scheduler import _events_from_rows

        db = Database(":memory:")
        game_id = "test_game_hot"
        segment_id = "seg_hot"
        db.upsert_game(game_id, "FakeGame", "any%")
        db.upsert_segment(Segment(
            id=segment_id, game_id=game_id, level_number=1,
            start_type=EndpointType.ENTRANCE, start_ordinal=0,
            end_type=EndpointType.GOAL, end_ordinal=0,
            description="hot seg",
        ))
        session_id = f"{game_id}:sess"
        db.create_session(session_id, game_id)

        db.log_event_attempt(EventAttempt(
            segment_id=segment_id, episode_id="ep_hot",
            outcome=AttemptOutcome.DIED, time_ms=5000,
            session_id=session_id, capture_run_id=None,
            source=AttemptSource.PRACTICE,
            chosen_allocator=None, invalidated=False,
            is_hot=True,
            created_at=datetime.fromisoformat("2026-05-26T00:00:00"),
        ))

        rows = db.get_segment_event_rows(segment_id)
        events = _events_from_rows(rows)

        assert len(events) == 1
        assert events[0].is_hot is True


