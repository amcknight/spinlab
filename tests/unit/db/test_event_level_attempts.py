"""Phase 0 schema + roll-up adapter tests.

Covers tests 1, 3, and 4 from the
``docs/superpowers/plans/2026-05-18-segments-v07-phase0-event-level-attempts.md``
test plan:

  1. Migration test — apply 0002, assert old columns gone, new columns
     present, model_state cleared.
  3. EpisodeView round-trip — events in, episode rolled back up, summed
     correctly.
  4. Aborted episode — deaths without a survived terminator surfaces as
     ``completed=False``, ``clean_tail_ms=None``.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from spinlab.db import Database
from spinlab.models import (
    DEFAULT_DEATH_PENALTY_MS,
    AttemptOutcome,
    AttemptSource,
    EventAttempt,
    Segment,
)


@pytest.fixture
def db_with_segment() -> Database:
    db = Database(":memory:")
    db.upsert_game("g1", "TestGame", "any%")
    db.create_session("sess1", "g1")
    db.upsert_segment(Segment(
        id="seg1", game_id="g1", level_number=1,
        start_type="entrance", start_ordinal=0,
        end_type="goal", end_ordinal=0,
        description="L1",
    ))
    return db


def _attempts_table_columns(db: Database) -> set[str]:
    return {
        row["name"] for row in db.conn.execute(
            "PRAGMA table_info(attempts)"
        ).fetchall()
    }


# ---------------------------------------------------------------------------
# Test 1 — Migration
# ---------------------------------------------------------------------------

def test_migration_drops_episode_columns_and_adds_event_columns():
    db = Database(":memory:")
    cols = _attempts_table_columns(db)
    # Removed by 0002:
    assert "completed" not in cols
    assert "deaths" not in cols
    assert "clean_tail_ms" not in cols
    # Added by 0002:
    assert "outcome" in cols
    assert "episode_id" in cols
    # Carried over:
    assert "segment_id" in cols
    assert "session_id" in cols
    assert "capture_run_id" in cols
    assert "time_ms" in cols
    assert "invalidated" in cols
    assert "chosen_allocator" in cols


def test_migration_clears_model_state():
    """0002 wipes model_state so rebuild_all_states regenerates from the new
    event-level shape via the legacy adapter."""
    db = Database(":memory:")
    # After fresh migration there should be nothing in model_state.
    row = db.conn.execute("SELECT COUNT(*) AS n FROM model_state").fetchone()
    assert row["n"] == 0


# ---------------------------------------------------------------------------
# Test 3 — EpisodeView round-trip
# ---------------------------------------------------------------------------

def test_event_roundtrip_completed_episode(db_with_segment):
    """Two deaths + one survive should roll up to deaths=2, time_ms includes
    the per-death penalty, clean_tail_ms equals the final event's time_ms."""
    db = db_with_segment
    ep = "epA"
    common = dict(
        segment_id="seg1", episode_id=ep, session_id="sess1",
        source=AttemptSource.PRACTICE,
    )
    db.log_event_attempt(EventAttempt(
        outcome=AttemptOutcome.DIED, time_ms=8000,
        created_at=datetime.now(UTC), **common,
    ))
    db.log_event_attempt(EventAttempt(
        outcome=AttemptOutcome.DIED, time_ms=7000,
        created_at=datetime.now(UTC), **common,
    ))
    db.log_event_attempt(EventAttempt(
        outcome=AttemptOutcome.SURVIVED, time_ms=20000,
        created_at=datetime.now(UTC), **common,
    ))

    rows = db.get_segment_attempts("seg1")
    assert len(rows) == 1
    ep_row = rows[0]
    assert ep_row["completed"] == 1
    assert ep_row["deaths"] == 2
    assert ep_row["clean_tail_ms"] == 20000
    # raw sum 35000 + 2 * 3200 penalty = 41400
    assert ep_row["time_ms"] == 35000 + 2 * DEFAULT_DEATH_PENALTY_MS


def test_event_roundtrip_clean_episode(db_with_segment):
    """One survived event = one episode with deaths=0, time_ms=clean_tail_ms."""
    db = db_with_segment
    db.log_event_attempt(EventAttempt(
        segment_id="seg1", episode_id="epClean", session_id="sess1",
        source=AttemptSource.PRACTICE,
        outcome=AttemptOutcome.SURVIVED, time_ms=15000,
        created_at=datetime.now(UTC),
    ))
    rows = db.get_segment_attempts("seg1")
    assert len(rows) == 1
    assert rows[0]["completed"] == 1
    assert rows[0]["deaths"] == 0
    assert rows[0]["time_ms"] == 15000
    assert rows[0]["clean_tail_ms"] == 15000


def test_compute_golds_excludes_invalidated_attempts(db_with_segment):
    """An invalidated (faster) clean clear must not win gold over a slower valid
    one — the invalidate button must actually clean the Best column."""
    db = db_with_segment
    # Valid slower clean clear.
    db.log_event_attempt(EventAttempt(
        segment_id="seg1", episode_id="epValid", session_id="sess1",
        source=AttemptSource.PRACTICE, outcome=AttemptOutcome.SURVIVED,
        time_ms=15000, created_at=datetime.now(UTC),
    ))
    # Invalidated faster clean clear (a mis-bounded outlier the user dropped).
    db.log_event_attempt(EventAttempt(
        segment_id="seg1", episode_id="epBad", session_id="sess1",
        source=AttemptSource.PRACTICE, outcome=AttemptOutcome.SURVIVED,
        time_ms=9000, created_at=datetime.now(UTC), invalidated=True,
    ))
    golds = db.compute_golds("g1")
    assert golds["seg1"]["gold_ms"] == 15000
    assert golds["seg1"]["clean_gold_ms"] == 15000


# ---------------------------------------------------------------------------
# Test 4 — Aborted episode
# ---------------------------------------------------------------------------

def test_aborted_episode_no_survived_event(db_with_segment):
    """An episode with deaths but no surviving event is completed=False, no
    clean tail. The legacy adapter must not pretend it succeeded."""
    db = db_with_segment
    common = dict(
        segment_id="seg1", episode_id="epAbort", session_id="sess1",
        source=AttemptSource.PRACTICE,
    )
    db.log_event_attempt(EventAttempt(
        outcome=AttemptOutcome.DIED, time_ms=5000,
        created_at=datetime.now(UTC), **common,
    ))
    db.log_event_attempt(EventAttempt(
        outcome=AttemptOutcome.DIED, time_ms=4000,
        created_at=datetime.now(UTC), **common,
    ))

    rows = db.get_segment_attempts("seg1")
    assert len(rows) == 1
    assert rows[0]["completed"] == 0
    assert rows[0]["deaths"] == 2
    assert rows[0]["clean_tail_ms"] is None
    # Incomplete episodes surface raw sum as time_ms — no penalty math.
    assert rows[0]["time_ms"] == 9000


def test_multiple_episodes_in_order(db_with_segment):
    """Episodes are ordered by their closing-event id (insertion order)."""
    db = db_with_segment
    db.log_event_attempt(EventAttempt(
        segment_id="seg1", episode_id="epOne", session_id="sess1",
        source=AttemptSource.PRACTICE,
        outcome=AttemptOutcome.SURVIVED, time_ms=10000,
        created_at=datetime.now(UTC),
    ))
    db.log_event_attempt(EventAttempt(
        segment_id="seg1", episode_id="epTwo", session_id="sess1",
        source=AttemptSource.PRACTICE,
        outcome=AttemptOutcome.SURVIVED, time_ms=20000,
        created_at=datetime.now(UTC),
    ))
    rows = db.get_segment_attempts("seg1")
    assert len(rows) == 2
    assert rows[0]["time_ms"] == 10000
    assert rows[1]["time_ms"] == 20000


def test_event_attempt_round_trip_with_is_hot(db_with_segment: "Database"):
    """EventAttempt with is_hot=True persists and round-trips through
    log_event_attempt + get_segment_event_rows."""
    db_with_segment.create_capture_run("run1", "g1", "test run")
    db_with_segment.log_event_attempt(EventAttempt(
        segment_id="seg1",
        episode_id="ep1",
        outcome=AttemptOutcome.SURVIVED,
        time_ms=1000,
        capture_run_id="run1",
        source=AttemptSource.REFERENCE,
        is_hot=True,
    ))
    rows = db_with_segment.get_segment_event_rows("seg1")
    assert len(rows) == 1
    assert rows[0]["is_hot"] == 1  # SQLite returns int, not bool


def test_event_attempt_default_is_cold(db_with_segment: "Database"):
    """An EventAttempt with no explicit is_hot persists as cold (0)."""
    db_with_segment.log_event_attempt(EventAttempt(
        segment_id="seg1",
        episode_id="ep1",
        outcome=AttemptOutcome.SURVIVED,
        time_ms=1000,
        session_id="sess1",
        source=AttemptSource.PRACTICE,
    ))
    rows = db_with_segment.get_segment_event_rows("seg1")
    assert rows[0]["is_hot"] == 0


# ---------------------------------------------------------------------------
# Surgery helpers
# ---------------------------------------------------------------------------

def test_get_attempt_segment_id(db_with_segment):
    db = db_with_segment
    db.log_event_attempt(EventAttempt(
        segment_id="seg1", episode_id="epX", session_id="sess1",
        source=AttemptSource.PRACTICE, outcome=AttemptOutcome.SURVIVED,
        time_ms=12000, created_at=datetime.now(UTC),
    ))
    att = db.get_segment_attempts("seg1")[0]
    assert db.get_attempt_segment_id(att["id"]) == "seg1"
    assert db.get_attempt_segment_id(999_999) is None


def test_invalidate_then_rebuild_updates_model_floor(db_with_segment):
    import json

    from spinlab.scheduler import Scheduler

    db = db_with_segment
    # Two clean clears: a fast 9s (the floor) and a slower 14s.
    for ep, t in [("epFast", 9000), ("epSlow", 14000)]:
        db.log_event_attempt(EventAttempt(
            segment_id="seg1", episode_id=ep, session_id="sess1",
            source=AttemptSource.PRACTICE, outcome=AttemptOutcome.SURVIVED,
            time_ms=t, created_at=datetime.now(UTC),
        ))
    sched = Scheduler(db, "g1")  # 3rd arg is an estimator-NAME string; default is fine
    sched.update_state_after_episode("seg1")
    out0 = json.loads(db.load_model_state("seg1", "em_suite_sampler")["output_json"])
    assert out0["total"]["floor_ms"] == 9000

    # Invalidate the fast episode, rebuild -> floor rises to 14000.
    fast_id = next(a["id"] for a in db.get_segment_attempts("seg1")
                   if a["clean_tail_ms"] == 9000)
    db.set_attempt_invalidated(fast_id, True)
    sched.update_state_after_episode("seg1")
    out1 = json.loads(db.load_model_state("seg1", "em_suite_sampler")["output_json"])
    assert out1["total"]["floor_ms"] == 14000
