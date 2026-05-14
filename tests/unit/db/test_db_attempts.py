"""Tests for db/attempts.py query methods."""

import pytest

from spinlab.db import Database
from spinlab.models import Attempt, Segment


@pytest.fixture
def db_with_attempts(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    db.upsert_game("g1", "Game", "any%")
    # Two segments
    for i in (1, 2):
        db.upsert_segment(Segment(
            id=f"s{i}", game_id="g1", level_number=i,
            start_type="entrance", start_ordinal=0,
            end_type="checkpoint", end_ordinal=0,
        ))
    db.create_session("sess1", "g1")
    # s1: 3 completed, 1 incomplete
    for t in [12000, 11000, 10000]:
        db.log_attempt(Attempt(
            segment_id="s1", session_id="sess1", completed=True,
            time_ms=t, deaths=0, clean_tail_ms=t,
        ))
    db.log_attempt(Attempt(
        segment_id="s1", session_id="sess1", completed=False,
        time_ms=None, deaths=1,
    ))
    # s2: 2 completed with deaths
    db.log_attempt(Attempt(
        segment_id="s2", session_id="sess1", completed=True,
        time_ms=20000, deaths=2, clean_tail_ms=8000,
    ))
    db.log_attempt(Attempt(
        segment_id="s2", session_id="sess1", completed=True,
        time_ms=18000, deaths=1, clean_tail_ms=9000,
    ))
    return db


class TestGetRecentAttempts:
    def test_returns_joined_rows(self, db_with_attempts):
        recent = db_with_attempts.get_recent_attempts("g1", limit=3)
        assert len(recent) == 3
        # Most recent first
        assert recent[0]["segment_id"] == "s2"
        # Has segment join fields
        assert "level_number" in recent[0]
        assert "start_type" in recent[0]

    def test_limit_respected(self, db_with_attempts):
        recent = db_with_attempts.get_recent_attempts("g1", limit=2)
        assert len(recent) == 2

    def test_wrong_game_returns_empty(self, db_with_attempts):
        recent = db_with_attempts.get_recent_attempts("nonexistent")
        assert recent == []


class TestGetAllAttemptsBySegment:
    def test_groups_by_segment(self, db_with_attempts):
        grouped = db_with_attempts.get_all_attempts_by_segment("g1")
        assert "s1" in grouped
        assert "s2" in grouped
        assert len(grouped["s1"]) == 4
        assert len(grouped["s2"]) == 2

    def test_ordered_by_created_at(self, db_with_attempts):
        grouped = db_with_attempts.get_all_attempts_by_segment("g1")
        times_s1 = [a["time_ms"] for a in grouped["s1"] if a["completed"]]
        # Should be in insertion order (chronological)
        assert times_s1 == [12000, 11000, 10000]


class TestGetSegmentAttempts:
    def test_returns_all_for_segment(self, db_with_attempts):
        rows = db_with_attempts.get_segment_attempts("s2")
        assert len(rows) == 2
        assert rows[0]["deaths"] == 2
        assert rows[0]["clean_tail_ms"] == 8000


def test_attempt_exists(tmp_path):
    from datetime import UTC, datetime

    from spinlab.db import Database
    from spinlab.models import Attempt, AttemptSource, Segment

    db = Database(str(tmp_path / "t.db"))
    db.upsert_game("g", "G", "any%")
    db.create_session("sess1", "g")
    db.upsert_segment(Segment(
        id="s1", game_id="g", level_number=1,
        start_type="entrance", start_ordinal=0,
        end_type="goal", end_ordinal=0,
    ))
    db.log_attempt(Attempt(
        segment_id="s1", session_id="sess1", completed=True,
        time_ms=1000, source=AttemptSource.PRACTICE,
        created_at=datetime.now(UTC),
    ))
    aid_row = db.conn.execute("SELECT id FROM attempts ORDER BY id DESC LIMIT 1").fetchone()
    aid = aid_row[0]

    assert db.attempt_exists(aid) is True
    assert db.attempt_exists(999999) is False
