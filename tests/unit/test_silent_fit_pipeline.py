"""End-to-end (in-process) test: events → DB → scheduler → segment_fits row."""
from __future__ import annotations

import pytest

pytest.importorskip("jax")
pytest.importorskip("numpyro")

from spinlab.db import Database
from spinlab.models import Attempt
from spinlab.scheduler import Scheduler


@pytest.fixture()
def seeded_db(tmp_path):
    db = Database(tmp_path / "t.db")
    db.conn.execute(
        "INSERT INTO games (id, name, category, created_at) "
        "VALUES ('g1', 'Test', 'Any%', '2026-05-19T00:00:00Z')"
    )
    db.conn.execute(
        "INSERT INTO segments (id, game_id, level_number, "
        "start_type, end_type, created_at, updated_at) "
        "VALUES ('s1', 'g1', 1, 'entrance', 'exit', "
        "'2026-05-19T00:00:00Z', '2026-05-19T00:00:00Z')"
    )
    db.conn.commit()
    return db


def test_episode_close_writes_segment_fit(seeded_db):
    """After one closed episode, a segment_fits row exists for that segment."""
    sched = Scheduler(seeded_db, game_id="g1")
    for i in range(30):
        sched.process_attempt(
            segment_id="s1", time_ms=20000, completed=True,
        )
    fit = seeded_db.load_latest_segment_fit("s1", "segment_fit")
    assert fit is not None
    assert fit["schema"] == "segments-v1"
    assert fit["n_attempts"] == 30


def test_subsequent_episode_warm_starts_from_previous(seeded_db, monkeypatch):
    """The second close should pass prev_result= so refit_segment runs
    the warm-start path."""
    from spinlab import scheduler as sched_mod

    calls: list[dict] = []
    real_refit = sched_mod._refit_segment

    def spy_refit(attempts, *, segment_id, prev_result=None):
        calls.append({"n": len(attempts), "warm": prev_result is not None})
        return real_refit(attempts, segment_id=segment_id, prev_result=prev_result)

    monkeypatch.setattr(sched_mod, "_refit_segment", spy_refit)

    # Seed with _MIN_EVENTS_FOR_FIT-1 attempts (below the fit floor) so
    # those don't trigger refit, then make two more calls above the
    # floor to exercise cold → warm-start transition.
    sched = Scheduler(seeded_db, game_id="g1")
    for _ in range(sched_mod._MIN_EVENTS_FOR_FIT - 1):
        sched.process_attempt(
            segment_id="s1", time_ms=20000, completed=True,
        )
    assert calls == []  # below-floor attempts produced no fits
    for _ in range(2):
        sched.process_attempt(
            segment_id="s1", time_ms=20000, completed=True,
        )

    # First above-floor call: cold (no prev). Second: warm (prev present).
    assert [c["warm"] for c in calls] == [False, True]


def test_silent_fit_skipped_cleanly_without_extras(seeded_db, monkeypatch):
    """If the [fits] extra is not installed, the scheduler must NOT crash
    on episode close; existing estimators stay green and no segment_fits
    row is written."""
    from spinlab import scheduler as sched_mod

    monkeypatch.setattr(sched_mod, "_refit_segment", None)

    sched = Scheduler(seeded_db, game_id="g1")
    sched.process_attempt(
        segment_id="s1", time_ms=20000, completed=True,
    )
    assert seeded_db.load_latest_segment_fit("s1", "segment_fit") is None
