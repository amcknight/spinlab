"""Tests for dashboard-specific DB queries."""
import pytest
from spinlab.db import Database
from spinlab.models import Segment, Attempt


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    d.upsert_game("test_game", "Test Game", "any%")
    return d


@pytest.fixture
def tmp_db(tmp_path):
    return Database(tmp_path / "test.db")


def _make_segment(db, game_id, level, start_type="entrance", start_ord=0,
                  end_type="goal", end_ord=0, desc="", ordinal=1, ref_id=None):
    seg = Segment(
        id=Segment.make_id(game_id, level, start_type, start_ord, end_type, end_ord),
        game_id=game_id, level_number=level,
        start_type=start_type, start_ordinal=start_ord,
        end_type=end_type, end_ordinal=end_ord,
        description=desc, ordinal=ordinal, reference_id=ref_id,
    )
    db.upsert_segment(seg)
    return seg


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


def test_get_segment_attempt_count(db):
    seg = _make_segment(db, "test_game", 1)
    db.create_session("sess1", "test_game")
    for _ in range(3):
        db.log_attempt(Attempt(
            segment_id=seg.id, session_id="sess1", completed=True,
            time_ms=1000,
        ))
    db.log_attempt(Attempt(
        segment_id=seg.id, session_id="other_sess", completed=True,
        time_ms=1000,
    ))
    assert db.get_segment_attempt_count(seg.id, "sess1") == 3


def test_get_recent_attempts(db):
    seg = _make_segment(db, "test_game", 1, desc="Level 1")
    db.create_session("sess1", "test_game")
    for i in range(10):
        db.log_attempt(Attempt(
            segment_id=seg.id, session_id="sess1", completed=(i % 2 == 0),
            time_ms=1000 + i * 100,
        ))
    results = db.get_recent_attempts("test_game", limit=5)
    assert len(results) == 5
    assert results[0]["time_ms"] == 1900
    assert results[0]["description"] == "Level 1"


def test_get_all_segments_with_model(db):
    _make_segment(db, "test_game", 1, ordinal=1)
    _make_segment(db, "test_game", 2, end_type="checkpoint", end_ord=1, ordinal=2)
    results = db.get_all_segments_with_model("test_game")
    assert len(results) == 2
    assert results[0]["level_number"] <= results[1]["level_number"]
    assert "id" in results[0]  # segment columns present


def test_segments_ordered_by_ordinal(tmp_path):
    """get_all_segments_with_model should return segments ordered by ordinal."""
    db = Database(tmp_path / "test.db")
    db.upsert_game("g", "Game", "any%")

    # Insert segments with ordinals out of level_number order
    for level, ordinal in [(30, 1), (10, 2), (20, 3)]:
        _make_segment(db, "g", level, ordinal=ordinal)

    rows = db.get_all_segments_with_model("g")
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


class TestModelStateDB:
    def test_save_and_load_model_state(self, tmp_path):
        db = Database(str(tmp_path / "test.db"))
        db.upsert_game("g1", "Game", "any%")
        seg = _make_segment(db, "g1", 1)

        db.save_model_state(seg.id, "kalman", '{"mu": 15.0}', '{"expected_time_ms": 15000.0}')
        row = db.load_model_state(seg.id, "kalman")
        assert row is not None
        assert row["estimator"] == "kalman"
        assert row["state_json"] == '{"mu": 15.0}'
        assert row["output_json"] == '{"expected_time_ms": 15000.0}'

    def test_load_missing_returns_none(self, tmp_path):
        db = Database(str(tmp_path / "test.db"))
        assert db.load_model_state("nonexistent") is None


def test_reset_game_data_scoped(tmp_db):
    """reset_game_data should only delete data for the specified game."""
    tmp_db.upsert_game("g1", "Game 1", "any%")
    tmp_db.upsert_game("g2", "Game 2", "any%")
    s1 = _make_segment(tmp_db, "g1", 1)
    s2 = _make_segment(tmp_db, "g2", 1)
    tmp_db.create_session("s1", "g1")
    tmp_db.create_session("s2", "g2")
    tmp_db.log_attempt(Attempt(segment_id=s1.id, time_ms=5000, completed=True, session_id="s1"))
    tmp_db.log_attempt(Attempt(segment_id=s2.id, time_ms=6000, completed=True, session_id="s2"))

    tmp_db.reset_game_data("g1")

    # g1 data gone
    assert tmp_db.get_recent_attempts("g1") == []
    assert tmp_db.get_session_history("g1") == []
    # g2 data intact
    assert len(tmp_db.get_recent_attempts("g2")) == 1
    assert len(tmp_db.get_session_history("g2")) == 1
