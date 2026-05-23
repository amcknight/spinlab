"""Tests for DatabaseCore.get_recently_played_games."""
import pytest

from spinlab.db import Database


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "t.db")


class TestGetRecentlyPlayedGames:
    def test_empty_when_no_games(self, db):
        assert db.get_recently_played_games() == []

    def test_returns_game_names(self, db):
        db.upsert_game("g1", "Love Yourself", "any%")
        result = db.get_recently_played_games()
        assert result == ["Love Yourself"]

    def test_most_recently_accessed_first(self, db):
        db.upsert_game("g1", "First", "any%")
        db.upsert_game("g2", "Second", "any%")
        # Force deterministic ordering.
        db.conn.execute("UPDATE games SET last_accessed='2020-01-01' WHERE id='g1'")
        db.conn.execute("UPDATE games SET last_accessed='2020-01-02' WHERE id='g2'")
        assert db.get_recently_played_games() == ["Second", "First"]

    def test_respects_limit(self, db):
        for i in range(5):
            db.upsert_game(f"g{i}", f"Game {i}", "any%")
        assert len(db.get_recently_played_games(limit=2)) == 2

    def test_upsert_updates_last_accessed(self, db):
        db.upsert_game("g1", "Alpha", "any%")
        db.upsert_game("g2", "Beta", "any%")
        # Re-access g1 by upserting it again; it should now be most recent.
        db.conn.execute("UPDATE games SET last_accessed='2020-01-01' WHERE id='g1'")
        db.conn.execute("UPDATE games SET last_accessed='2020-01-02' WHERE id='g2'")
        db.upsert_game("g1", "Alpha", "any%")  # updates last_accessed to now
        result = db.get_recently_played_games(limit=2)
        assert result[0] == "Alpha"
