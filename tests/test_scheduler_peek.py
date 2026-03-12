"""Tests for Scheduler.peek_next_n()."""
import pytest
from spinlab.db import Database
from spinlab.models import Split
from spinlab.scheduler import Scheduler


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    d.upsert_game("test_game", "Test Game", "any%")
    return d


def seed_splits(db, n):
    for i in range(n):
        s = Split(id=f"s{i}", game_id="test_game", level_number=i,
                  room_id=0, goal="normal", state_path=f"/state_{i}.mss")
        db.upsert_split(s)
        db.ensure_schedule(s.id)


def test_peek_returns_requested_count(db):
    seed_splits(db, 5)
    sched = Scheduler(db, "test_game")
    result = sched.peek_next_n(3)
    assert len(result) == 3


def test_peek_returns_less_if_fewer_available(db):
    seed_splits(db, 2)
    sched = Scheduler(db, "test_game")
    result = sched.peek_next_n(5)
    assert len(result) == 2


def test_peek_returns_empty_with_no_splits(db):
    sched = Scheduler(db, "test_game")
    result = sched.peek_next_n(3)
    assert result == []


def test_peek_returns_split_ids(db):
    seed_splits(db, 3)
    sched = Scheduler(db, "test_game")
    result = sched.peek_next_n(3)
    assert all(isinstance(sid, str) for sid in result)
    assert all(sid.startswith("s") for sid in result)
