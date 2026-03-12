"""Tests for the scheduler coordinator (estimator + allocator)."""
import json
import pytest
from spinlab.db import Database
from spinlab.scheduler import Scheduler


@pytest.fixture
def db_with_splits(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    db.upsert_game("g1", "Game", "any%")
    from spinlab.models import Split
    for i, goal in enumerate(["normal", "key", "secret"], start=1):
        split = Split(
            id=f"g1:{i}:1:{goal}",
            game_id="g1",
            level_number=i,
            room_id=1,
            goal=goal,
            description=f"Level {i}",
            state_path=f"/states/{i}.mss",
            reference_time_ms=10000 + i * 1000,
            strat_version=1,
        )
        db.upsert_split(split)
    return db


class TestSchedulerPickNext:
    def test_pick_next_returns_split_with_model(self, db_with_splits):
        sched = Scheduler(db_with_splits, "g1")
        result = sched.pick_next()
        # No attempts yet, all marginal returns are equal (default d/mu)
        assert result is not None
        assert result.split_id.startswith("g1:")

    def test_pick_next_no_splits_returns_none(self, tmp_path):
        db = Database(str(tmp_path / "test.db"))
        db.upsert_game("g1", "Game", "any%")
        sched = Scheduler(db, "g1")
        assert sched.pick_next() is None


class TestSchedulerProcessAttempt:
    def test_process_attempt_creates_model_state(self, db_with_splits):
        sched = Scheduler(db_with_splits, "g1")
        split_id = "g1:1:1:normal"
        sched.process_attempt(split_id, time_ms=12000, completed=True)
        row = db_with_splits.load_model_state(split_id)
        assert row is not None
        state = json.loads(row["state_json"])
        assert state["mu"] == pytest.approx(12.0)  # 12000ms → 12.0s
        assert state["n_completed"] == 1

    def test_process_attempt_incomplete(self, db_with_splits):
        sched = Scheduler(db_with_splits, "g1")
        split_id = "g1:1:1:normal"
        # First: completed attempt to init state
        sched.process_attempt(split_id, time_ms=12000, completed=True)
        # Second: incomplete attempt
        sched.process_attempt(split_id, time_ms=5000, completed=False)
        row = db_with_splits.load_model_state(split_id)
        state = json.loads(row["state_json"])
        assert state["n_completed"] == 1  # unchanged
        assert state["n_attempts"] == 2   # incremented


class TestSchedulerPeek:
    def test_peek_next_n(self, db_with_splits):
        sched = Scheduler(db_with_splits, "g1")
        results = sched.peek_next_n(3)
        assert len(results) == 3
        assert all(isinstance(r, str) for r in results)


class TestSchedulerSwitch:
    def test_switch_allocator(self, db_with_splits):
        sched = Scheduler(db_with_splits, "g1")
        sched.switch_allocator("random")
        assert sched.allocator.name == "random"

    def test_switch_unknown_allocator_raises(self, db_with_splits):
        sched = Scheduler(db_with_splits, "g1")
        with pytest.raises(ValueError):
            sched.switch_allocator("nonexistent")
