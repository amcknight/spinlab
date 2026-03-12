"""Tests for legacy Scheduler stub (SM-2 replaced by Kalman allocator)."""
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


def test_peek_returns_empty_stub(db):
    """Legacy scheduler stub always returns empty — use Kalman allocator."""
    seed_splits(db, 5)
    sched = Scheduler(db, "test_game")
    result = sched.peek_next_n(3)
    assert result == []


def test_pick_next_returns_none_stub(db):
    seed_splits(db, 3)
    sched = Scheduler(db, "test_game")
    assert sched.pick_next() is None
