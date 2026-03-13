"""Tests for dashboard-specific DB queries."""
import pytest
from spinlab.db import Database
from spinlab.models import Split, Attempt


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    d.upsert_game("test_game", "Test Game", "any%")
    return d


@pytest.fixture
def tmp_db(tmp_path):
    return Database(tmp_path / "test.db")


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
    from spinlab.models import Split, Attempt
    split = Split(id="s1", game_id="test_game", level_number=1,
                  room_id=0, goal="normal")
    db.upsert_split(split)
    db.create_session("sess1", "test_game")
    for _ in range(3):
        db.log_attempt(Attempt(
            split_id="s1", session_id="sess1", completed=True,
            time_ms=1000,
        ))
    db.log_attempt(Attempt(
        split_id="s1", session_id="other_sess", completed=True,
        time_ms=1000,
    ))
    assert db.get_split_attempt_count("s1", "sess1") == 3


def test_get_recent_attempts(db):
    from spinlab.models import Split, Attempt
    split = Split(id="s1", game_id="test_game", level_number=1,
                  room_id=0, goal="normal", description="Level 1")
    db.upsert_split(split)
    db.create_session("sess1", "test_game")
    for i in range(10):
        db.log_attempt(Attempt(
            split_id="s1", session_id="sess1", completed=(i % 2 == 0),
            time_ms=1000 + i * 100,
        ))
    results = db.get_recent_attempts("test_game", limit=5)
    assert len(results) == 5
    assert results[0]["time_ms"] == 1900
    assert results[0]["goal"] == "normal"


def test_get_all_splits_with_model(db):
    from spinlab.models import Split
    s1 = Split(id="s1", game_id="test_game", level_number=1,
               room_id=0, goal="normal")
    s2 = Split(id="s2", game_id="test_game", level_number=2,
               room_id=0, goal="key")
    db.upsert_split(s1)
    db.upsert_split(s2)
    results = db.get_all_splits_with_model("test_game")
    assert len(results) == 2
    assert results[0]["level_number"] <= results[1]["level_number"]
    assert "estimator" in results[0]  # LEFT JOIN column exists


def test_splits_ordered_by_ordinal(tmp_path):
    """get_all_splits_with_model should return splits ordered by ordinal."""
    from spinlab.db import Database
    from spinlab.models import Split

    db = Database(tmp_path / "test.db")
    db.upsert_game("g", "Game", "any%")

    # Insert splits with ordinals out of level_number order
    for level, ordinal in [(30, 1), (10, 2), (20, 3)]:
        s = Split(id=f"g:{level}:0:normal", game_id="g",
                  level_number=level, room_id=0, goal="normal",
                  ordinal=ordinal)
        db.upsert_split(s)

    rows = db.get_all_splits_with_model("g")
    levels = [r["level_number"] for r in rows]
    assert levels == [30, 10, 20], f"Expected ordinal order [30,10,20], got {levels}"


def test_get_session_history(db):
    db.create_session("sess1", "test_game")
    db.end_session("sess1", 10, 8)
    db.create_session("sess2", "test_game")
    db.end_session("sess2", 5, 4)
    db.create_session("sess3", "test_game")  # still active
    results = db.get_session_history("test_game", limit=5)
    assert len(results) == 3
    assert results[0]["id"] == "sess3"


class TestSchemaMigration:
    def test_old_schedule_table_dropped_on_init(self, tmp_path):
        """If old DB has 'schedule' table, it gets dropped and replaced."""
        import sqlite3

        db_path = tmp_path / "test.db"
        # Create old-schema DB with schedule table
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE schedule (split_id TEXT PRIMARY KEY, ease_factor REAL)")
        conn.execute("INSERT INTO schedule VALUES ('s1', 2.5)")
        conn.commit()
        conn.close()

        # Init with new code should drop schedule, create model_state
        from spinlab.db import Database
        db = Database(str(db_path))

        # schedule table should be gone
        cur = db.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='schedule'")
        assert cur.fetchone() is None

        # model_state and allocator_config should exist
        cur = db.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='model_state'")
        assert cur.fetchone() is not None
        cur = db.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='allocator_config'")
        assert cur.fetchone() is not None


class TestModelStateDB:
    def test_save_and_load_model_state(self, tmp_path):
        from spinlab.db import Database
        db = Database(str(tmp_path / "test.db"))
        db.conn.execute("INSERT INTO games (id, name, category, created_at) VALUES ('g1', 'Game', 'any%', '2024-01-01')")
        db.conn.execute(
            "INSERT INTO splits (id, game_id, level_number, goal, description, strat_version, active, created_at, updated_at) "
            "VALUES ('s1', 'g1', 1, 'normal', 'test', 1, 1, '2024-01-01', '2024-01-01')"
        )
        db.conn.commit()

        db.save_model_state("s1", "kalman", '{"mu": 15.0}', 0.05)
        row = db.load_model_state("s1")
        assert row is not None
        assert row["estimator"] == "kalman"
        assert row["state_json"] == '{"mu": 15.0}'
        assert row["marginal_return"] == pytest.approx(0.05)

    def test_load_missing_returns_none(self, tmp_path):
        from spinlab.db import Database
        db = Database(str(tmp_path / "test.db"))
        assert db.load_model_state("nonexistent") is None


def test_reset_game_data_scoped(tmp_db):
    """reset_game_data should only delete data for the specified game."""
    tmp_db.upsert_game("g1", "Game 1", "any%")
    tmp_db.upsert_game("g2", "Game 2", "any%")
    s1 = Split(id="g1:1:1:normal", game_id="g1", level_number=1, room_id=1, goal="normal")
    s2 = Split(id="g2:1:1:normal", game_id="g2", level_number=1, room_id=1, goal="normal")
    tmp_db.upsert_split(s1)
    tmp_db.upsert_split(s2)
    tmp_db.create_session("s1", "g1")
    tmp_db.create_session("s2", "g2")
    tmp_db.log_attempt(Attempt(split_id="g1:1:1:normal", time_ms=5000, completed=True, session_id="s1"))
    tmp_db.log_attempt(Attempt(split_id="g2:1:1:normal", time_ms=6000, completed=True, session_id="s2"))

    tmp_db.reset_game_data("g1")

    # g1 data gone
    assert tmp_db.get_recent_attempts("g1") == []
    assert tmp_db.get_session_history("g1") == []
    # g2 data intact
    assert len(tmp_db.get_recent_attempts("g2")) == 1
    assert len(tmp_db.get_session_history("g2")) == 1
