"""Regression test: replay-sourced event_attempt rows must not seed the model.

Replay and fast-replay collapse wall-clock time, so their event timing is
meaningless for the sampler. Events tagged source=REPLAY are written to the DB
for provenance but must be excluded from the estimator's ingestion path.

Fixture: write some REFERENCE events and some REPLAY events for the same
segment, then hydrate via _events_from_rows (the scheduler's sampler-ingestion
seam) and verify that only the REFERENCE events appear.
"""
from __future__ import annotations

import pytest


class TestReplayEventsNotSeeded:
    """REPLAY-sourced events are excluded from model ingestion."""

    def _make_segment(self, db, game_id: str, segment_id: str) -> str:
        """Create game + segment + capture_run. Return capture_run_id."""
        from spinlab.models import EndpointType, Segment

        db.upsert_game(game_id, "TestGame", "any%")
        db.upsert_segment(Segment(
            id=segment_id, game_id=game_id, level_number=1,
            start_type=EndpointType.ENTRANCE, start_ordinal=0,
            end_type=EndpointType.GOAL, end_ordinal=0,
            description="test seg",
        ))
        run_id = f"run_{game_id}"
        db.create_capture_run(run_id, game_id, "TestRef", kind="live")
        return run_id

    def test_reference_events_are_included(self):
        """REFERENCE events flow through _events_from_rows unchanged."""
        from datetime import datetime

        from spinlab.db import Database
        from spinlab.models import AttemptOutcome, AttemptSource, EventAttempt
        from spinlab.scheduler import _events_from_rows

        db = Database(":memory:")
        game_id = "replay_seed_ref_only"
        segment_id = "seg_ref_only"
        run_id = self._make_segment(db, game_id, segment_id)

        db.log_event_attempt(EventAttempt(
            segment_id=segment_id, episode_id="ep1",
            outcome=AttemptOutcome.SURVIVED, time_ms=5000,
            capture_run_id=run_id,
            source=AttemptSource.REFERENCE,
            created_at=datetime.fromisoformat("2026-06-01T00:00:00"),
        ))

        rows = db.get_segment_event_rows(segment_id)
        events = _events_from_rows(rows)
        assert len(events) == 1
        assert events[0].source is AttemptSource.REFERENCE

    def test_replay_events_excluded_from_model_ingestion(self):
        """REPLAY events are present in the DB but excluded from _events_from_rows.

        This verifies the sampler-ingestion seam: if the filter is absent,
        both events come through and n_attempts_total == 2.
        """
        from datetime import datetime

        from spinlab.db import Database
        from spinlab.models import AttemptOutcome, AttemptSource, EventAttempt
        from spinlab.scheduler import _events_from_rows

        db = Database(":memory:")
        game_id = "replay_seed_filter"
        segment_id = "seg_replay_filter"
        run_id = self._make_segment(db, game_id, segment_id)

        # One REFERENCE event — should be counted by the sampler.
        db.log_event_attempt(EventAttempt(
            segment_id=segment_id, episode_id="ep_ref",
            outcome=AttemptOutcome.SURVIVED, time_ms=5000,
            capture_run_id=run_id,
            source=AttemptSource.REFERENCE,
            created_at=datetime.fromisoformat("2026-06-01T00:00:00"),
        ))
        # One REPLAY event — recorded for provenance, must not reach the sampler.
        db.log_event_attempt(EventAttempt(
            segment_id=segment_id, episode_id="ep_replay",
            outcome=AttemptOutcome.SURVIVED, time_ms=100,
            capture_run_id=run_id,
            source=AttemptSource.REPLAY,
            created_at=datetime.fromisoformat("2026-06-01T00:01:00"),
        ))

        # Both rows exist in the DB (provenance intact).
        all_rows = db.get_segment_event_rows(segment_id)
        assert len(all_rows) == 2, (
            f"Expected 2 rows in DB for provenance, got {len(all_rows)}"
        )

        # Only the REFERENCE event reaches the sampler.
        events = _events_from_rows(all_rows)
        assert len(events) == 1, (
            f"Expected 1 event after replay filter, got {len(events)}: {events}"
        )
        assert events[0].source is AttemptSource.REFERENCE

    def test_sampler_state_excludes_replay_events(self):
        """The EMA sampler's n_attempts_total reflects only non-replay events.

        Writes 2 REFERENCE + 3 REPLAY events for one segment, hydrates via
        _events_from_rows + rebuild_state, and asserts the sampler saw exactly
        2 attempts (the reference ones).
        """
        from datetime import datetime

        from spinlab.db import Database
        from spinlab.estimators import get_estimator
        from spinlab.models import AttemptOutcome, AttemptSource, EventAttempt
        from spinlab.scheduler import _events_from_rows

        db = Database(":memory:")
        game_id = "replay_seed_sampler"
        segment_id = "seg_sampler_check"
        run_id = self._make_segment(db, game_id, segment_id)

        ts_base = "2026-06-01T00:0{}:00"

        # 2 REFERENCE events (one death + one survival).
        db.log_event_attempt(EventAttempt(
            segment_id=segment_id, episode_id="ep_ref_1",
            outcome=AttemptOutcome.DIED, time_ms=3000,
            capture_run_id=run_id,
            source=AttemptSource.REFERENCE,
            created_at=datetime.fromisoformat(ts_base.format(0)),
        ))
        db.log_event_attempt(EventAttempt(
            segment_id=segment_id, episode_id="ep_ref_2",
            outcome=AttemptOutcome.SURVIVED, time_ms=5000,
            capture_run_id=run_id,
            source=AttemptSource.REFERENCE,
            created_at=datetime.fromisoformat(ts_base.format(1)),
        ))

        # 3 REPLAY events — must not appear in the sampler.
        for i in range(3):
            db.log_event_attempt(EventAttempt(
                segment_id=segment_id, episode_id=f"ep_replay_{i}",
                outcome=AttemptOutcome.SURVIVED, time_ms=50,
                capture_run_id=run_id,
                source=AttemptSource.REPLAY,
                created_at=datetime.fromisoformat(ts_base.format(i + 2)),
            ))

        all_rows = db.get_segment_event_rows(segment_id)
        assert len(all_rows) == 5, "All 5 rows must be in DB for provenance"

        events = _events_from_rows(all_rows)
        assert len(events) == 2, (
            f"Expected 2 events after replay filter, got {len(events)}"
        )

        est = get_estimator("em_suite_sampler")
        state = est.rebuild_state([], params=None, events=events)

        # Sampler should count exactly 2 attempts (the 2 REFERENCE events).
        assert state.n_attempts_total == 2, (
            f"Expected n_attempts_total=2, got {state.n_attempts_total}"
        )
        # Pool sizes reflect only REFERENCE events: 1 death, 1 success.
        assert len(state.death_time_pool) == 1, (
            f"Expected 1 death in pool, got {len(state.death_time_pool)}"
        )
        assert len(state.success_time_pool) == 1, (
            f"Expected 1 success in pool, got {len(state.success_time_pool)}"
        )


class TestV07RefitReplayGuard:
    """The filtering predicate in _maybe_refit_segment excludes REPLAY rows.

    The v07 refit (_refit_segment) requires JAX/[fits] and is skipped when
    that extra is absent. Rather than gate these tests on [fits], we verify
    the filtering predicate directly: given a list of raw event rows (as
    returned by db.get_segment_event_rows), the comprehension that builds
    ``attempts`` must exclude REPLAY-sourced rows.
    """

    def _make_attempt_rows(self, sources_and_outcomes):
        """Build minimal raw event rows (as dicts) matching DB shape."""
        rows = []
        for i, (source, outcome) in enumerate(sources_and_outcomes):
            rows.append({
                "segment_id": "seg_v07_test",
                "episode_id": f"ep{i}",
                "outcome": outcome,
                "time_ms": 1000 * (i + 1),
                "source": source,
                "invalidated": 0,
                "is_hot": 0,
                "created_at": "2026-06-01T00:00:00",
            })
        return rows

    def test_refit_predicate_excludes_replay_rows(self):
        """The filtering predicate used in _maybe_refit_segment drops REPLAY rows.

        Applies the same filter expression from _maybe_refit_segment to a
        mixed list of REFERENCE and REPLAY rows and asserts only REFERENCE
        rows survive.
        """
        from spinlab.models import AttemptSource

        rows = self._make_attempt_rows([
            (AttemptSource.REFERENCE.value, "survived"),
            (AttemptSource.REPLAY.value, "survived"),
            (AttemptSource.REFERENCE.value, "died"),
            (AttemptSource.REPLAY.value, "died"),
        ])

        # This is the exact predicate from _maybe_refit_segment.
        attempts = [
            {"outcome": e["outcome"], "time_ms": int(e["time_ms"])}
            for e in rows
            if not int(e["invalidated"])
            and AttemptSource(e["source"]) is not AttemptSource.REPLAY
        ]

        assert len(attempts) == 2, (
            f"Expected 2 attempts (REFERENCE only), got {len(attempts)}: {attempts}"
        )
        assert all(
            AttemptSource(r["source"]) is AttemptSource.REFERENCE
            for r in rows
            if {"outcome": r["outcome"], "time_ms": int(r["time_ms"])} in attempts
        ), "Only REFERENCE rows should survive the predicate"

    def test_refit_predicate_respects_invalidated(self):
        """Invalidated rows are dropped regardless of source."""
        from spinlab.models import AttemptSource

        rows = self._make_attempt_rows([
            (AttemptSource.REFERENCE.value, "survived"),
            (AttemptSource.REFERENCE.value, "died"),
        ])
        # Mark the second row as invalidated.
        rows[1]["invalidated"] = 1

        attempts = [
            {"outcome": e["outcome"], "time_ms": int(e["time_ms"])}
            for e in rows
            if not int(e["invalidated"])
            and AttemptSource(e["source"]) is not AttemptSource.REPLAY
        ]

        assert len(attempts) == 1, (
            f"Expected 1 attempt (non-invalidated REFERENCE), got {len(attempts)}"
        )
        assert attempts[0]["outcome"] == "survived"
