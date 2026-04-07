"""Regression test: stale estimator name in DB should not crash Scheduler."""
import pytest
from spinlab.db import Database
from spinlab.scheduler import Scheduler


def test_stale_estimator_falls_back_to_default(tmp_path):
    """Scheduler with a bogus saved estimator name falls back to 'kalman'."""
    db = Database(str(tmp_path / "test.db"))
    db.upsert_game("g1", "Test Game", "any%")
    db.save_allocator_config("estimator", "bogus_name_that_does_not_exist")

    scheduler = Scheduler(db, "g1")

    assert scheduler.estimator.name == "kalman"


def test_valid_saved_estimator_is_used(tmp_path):
    """Scheduler with a valid saved estimator name should use it."""
    db = Database(str(tmp_path / "test.db"))
    db.upsert_game("g1", "Test Game", "any%")
    db.save_allocator_config("estimator", "rolling_mean")

    scheduler = Scheduler(db, "g1")

    assert scheduler.estimator.name == "rolling_mean"


def test_no_saved_estimator_uses_default(tmp_path):
    """Scheduler with no saved estimator uses the constructor default."""
    db = Database(str(tmp_path / "test.db"))
    db.upsert_game("g1", "Test Game", "any%")

    scheduler = Scheduler(db, "g1")

    assert scheduler.estimator.name == "kalman"
