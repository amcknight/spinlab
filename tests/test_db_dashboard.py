"""Tests for dashboard-specific DB queries."""
import pytest
from spinlab.db import Database


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    d.upsert_game("test_game", "Test Game", "any%")
    return d


def test_get_current_session_returns_active(db):
    db.create_session("sess1", "test_game")
    result = db.get_current_session("test_game")
    assert result is not None
    assert result["id"] == "sess1"
    assert result["ended_at"] is None


def test_get_current_session_ignores_ended(db):
    db.create_session("sess1", "test_game")
    db.end_session("sess1", 5, 3)
    result = db.get_current_session("test_game")
    assert result is None


def test_get_split_attempt_count(db):
    from spinlab.models import Split, Attempt, Rating
    split = Split(id="s1", game_id="test_game", level_number=1,
                  room_id=0, goal="normal")
    db.upsert_split(split)
    db.create_session("sess1", "test_game")
    for _ in range(3):
        db.log_attempt(Attempt(
            split_id="s1", session_id="sess1", completed=True,
            time_ms=1000, rating=Rating.GOOD,
        ))
    db.log_attempt(Attempt(
        split_id="s1", session_id="other_sess", completed=True,
        time_ms=1000, rating=Rating.GOOD,
    ))
    assert db.get_split_attempt_count("s1", "sess1") == 3


def test_get_recent_attempts(db):
    from spinlab.models import Split, Attempt, Rating
    split = Split(id="s1", game_id="test_game", level_number=1,
                  room_id=0, goal="normal", description="Level 1")
    db.upsert_split(split)
    db.create_session("sess1", "test_game")
    for i in range(10):
        db.log_attempt(Attempt(
            split_id="s1", session_id="sess1", completed=(i % 2 == 0),
            time_ms=1000 + i * 100, rating=Rating.GOOD,
        ))
    results = db.get_recent_attempts("test_game", limit=5)
    assert len(results) == 5
    assert results[0]["time_ms"] == 1900
    assert results[0]["goal"] == "normal"


def test_get_all_splits_with_schedule(db):
    from spinlab.models import Split
    s1 = Split(id="s1", game_id="test_game", level_number=1,
               room_id=0, goal="normal")
    s2 = Split(id="s2", game_id="test_game", level_number=2,
               room_id=0, goal="key")
    db.upsert_split(s1)
    db.upsert_split(s2)
    db.ensure_schedule("s1")
    db.ensure_schedule("s2")
    results = db.get_all_splits_with_schedule("test_game")
    assert len(results) == 2
    assert "ease_factor" in results[0]
    assert results[0]["level_number"] <= results[1]["level_number"]


def test_get_session_history(db):
    db.create_session("sess1", "test_game")
    db.end_session("sess1", 10, 8)
    db.create_session("sess2", "test_game")
    db.end_session("sess2", 5, 4)
    db.create_session("sess3", "test_game")  # still active
    results = db.get_session_history("test_game", limit=5)
    assert len(results) == 3
    assert results[0]["id"] == "sess3"
