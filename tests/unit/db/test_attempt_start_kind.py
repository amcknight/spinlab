"""Migration 0007 — is_hot column + reference-run backfill."""
from __future__ import annotations

from pathlib import Path

import spinlab.db.migrations as _mig_pkg
from spinlab.db import Database

_BACKFILL_SQL = (
    Path(_mig_pkg.__file__).parent / "0007_attempt_start_kind.sql"
).read_text().split("-- BACKFILL")[1]


def _columns(db: Database, table: str) -> set[str]:
    return {r["name"] for r in db.conn.execute(f"PRAGMA table_info({table})").fetchall()}


def test_column_added_with_cold_default():
    db = Database(":memory:")
    assert "is_hot" in _columns(db, "attempts")
    # Insert a row without specifying is_hot; default must be 0 (cold).
    db.upsert_game("g1", "G", "any%")
    db.create_session("s1", "g1")
    from spinlab.models import Segment
    db.upsert_segment(Segment(
        id="seg1", game_id="g1", level_number=1,
        start_type="entrance", start_ordinal=0,
        end_type="goal", end_ordinal=0, description="L1",
    ))
    db.conn.execute(
        "INSERT INTO attempts (segment_id, session_id, episode_id, outcome, "
        "time_ms, source, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("seg1", "s1", "ep1", "survived", 1000, "practice", "2026-05-26T00:00:00"),
    )
    row = db.conn.execute("SELECT is_hot FROM attempts WHERE episode_id = 'ep1'").fetchone()
    assert row["is_hot"] == 0


def _insert_attempt(
    db: Database, *, segment_id: str, capture_run_id: str | None = None,
    session_id: str | None = None, episode_id: str, outcome: str,
    source: str = "reference", created_at: str = "2026-05-26T00:00:00",
) -> int:
    cur = db.conn.execute(
        "INSERT INTO attempts (segment_id, session_id, capture_run_id, episode_id, "
        "outcome, time_ms, source, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (segment_id, session_id, capture_run_id, episode_id, outcome, 1000, source, created_at),
    )
    return cur.lastrowid  # type: ignore[return-value]


def _seed_run(db: Database) -> str:
    """Create a capture_run + two segments for use in backfill tests."""
    from spinlab.models import Segment
    db.upsert_game("g1", "G", "any%")
    db.create_capture_run("run1", "g1", "test run")
    for sid in ("segA", "segB"):
        db.upsert_segment(Segment(
            id=sid, game_id="g1", level_number=1,
            start_type="entrance", start_ordinal=0,
            end_type="goal", end_ordinal=0, description=sid,
        ))
    return "run1"


def test_backfill_hot_when_prior_attempt_survived_different_episode():
    """A reference attempt that is the first of its episode is HOT iff the
    immediately prior attempt in the same capture_run survived and was from
    a different episode."""
    db = Database(":memory:")
    run_id = _seed_run(db)
    a1 = _insert_attempt(db, segment_id="segA", capture_run_id=run_id,
                         episode_id="epA", outcome="survived")
    a2 = _insert_attempt(db, segment_id="segB", capture_run_id=run_id,
                         episode_id="epB", outcome="survived")
    backfill = _BACKFILL_SQL
    db.conn.executescript(backfill)
    db.conn.commit()

    row_a1 = db.conn.execute("SELECT is_hot FROM attempts WHERE id = ?", (a1,)).fetchone()
    row_a2 = db.conn.execute("SELECT is_hot FROM attempts WHERE id = ?", (a2,)).fetchone()
    assert row_a1["is_hot"] == 0, "first attempt of run is cold (level start)"
    assert row_a2["is_hot"] == 1, "first attempt of new segment after survival is hot"


def test_backfill_cold_when_prior_attempt_died():
    """If the immediately prior attempt in the same run was a death, the
    next first-of-episode is COLD (post-death respawn)."""
    db = Database(":memory:")
    run_id = _seed_run(db)
    _insert_attempt(db, segment_id="segA", capture_run_id=run_id,
                    episode_id="epA", outcome="died")
    a2 = _insert_attempt(db, segment_id="segA", capture_run_id=run_id,
                         episode_id="epA2", outcome="survived")

    backfill = _BACKFILL_SQL
    db.conn.executescript(backfill)
    db.conn.commit()

    row_a2 = db.conn.execute("SELECT is_hot FROM attempts WHERE id = ?", (a2,)).fetchone()
    assert row_a2["is_hot"] == 0


def test_backfill_cold_for_practice_attempts():
    """Practice attempts are always cold even after a prior survival."""
    db = Database(":memory:")
    db.upsert_game("g1", "G", "any%")
    db.create_session("sess1", "g1")
    from spinlab.models import Segment
    db.upsert_segment(Segment(
        id="seg1", game_id="g1", level_number=1,
        start_type="entrance", start_ordinal=0,
        end_type="goal", end_ordinal=0, description="L1",
    ))
    a1 = _insert_attempt(db, segment_id="seg1", session_id="sess1",
                         episode_id="ep1", outcome="survived", source="practice")
    a2 = _insert_attempt(db, segment_id="seg1", session_id="sess1",
                         episode_id="ep2", outcome="survived", source="practice")

    backfill = _BACKFILL_SQL
    db.conn.executescript(backfill)
    db.conn.commit()

    for aid in (a1, a2):
        row = db.conn.execute("SELECT is_hot FROM attempts WHERE id = ?", (aid,)).fetchone()
        assert row["is_hot"] == 0


def test_backfill_subsequent_attempts_in_same_episode_are_cold():
    """Within one episode, the first attempt may be hot but all post-death
    respawns are cold."""
    db = Database(":memory:")
    run_id = _seed_run(db)
    _insert_attempt(db, segment_id="segA", capture_run_id=run_id,
                    episode_id="epA", outcome="survived")
    first_b = _insert_attempt(db, segment_id="segB", capture_run_id=run_id,
                              episode_id="epB", outcome="died")
    second_b = _insert_attempt(db, segment_id="segB", capture_run_id=run_id,
                               episode_id="epB", outcome="died")
    third_b = _insert_attempt(db, segment_id="segB", capture_run_id=run_id,
                              episode_id="epB", outcome="survived")

    backfill = _BACKFILL_SQL
    db.conn.executescript(backfill)
    db.conn.commit()

    rows = {r["id"]: r["is_hot"] for r in db.conn.execute(
        "SELECT id, is_hot FROM attempts WHERE episode_id = 'epB'"
    ).fetchall()}
    assert rows[first_b] == 1, "first life of segB after surviving segA is hot"
    assert rows[second_b] == 0, "second life (post-death respawn) is cold"
    assert rows[third_b] == 0, "third life (post-death respawn) is cold"
