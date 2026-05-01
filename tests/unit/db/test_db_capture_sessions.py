"""Tests for CaptureSessionsMixin."""
import pytest
from spinlab.db import Database


@pytest.fixture
def db():
    d = Database(":memory:")
    d.upsert_game("smw", "Super Mario World", "any%")
    d.create_capture_run("run_1", "smw", "Test Run", draft=True)
    yield d
    d.close()


def test_create_and_get_capture_session(db):
    db.create_capture_session(
        session_id="sess_1", capture_run_id="run_1",
        ordinal=1, spinrec_path="/tmp/sess_1.spinrec",
    )
    sess = db.get_capture_session("sess_1")
    assert sess is not None
    assert sess["id"] == "sess_1"
    assert sess["capture_run_id"] == "run_1"
    assert sess["ordinal"] == 1
    assert sess["spinrec_path"] == "/tmp/sess_1.spinrec"
    assert sess["started_at"] is not None
    assert sess["ended_at"] is None
    assert sess["end_reason"] is None


def test_get_capture_session_missing_returns_none(db):
    assert db.get_capture_session("nonexistent") is None


def test_end_capture_session_sets_ended_at_and_reason(db):
    db.create_capture_session("sess_1", "run_1", 1, "/tmp/x.spinrec")
    db.end_capture_session("sess_1", end_reason="stopped")
    sess = db.get_capture_session("sess_1")
    assert sess["ended_at"] is not None
    assert sess["end_reason"] == "stopped"


def test_end_capture_session_is_idempotent(db):
    db.create_capture_session("sess_1", "run_1", 1, "/tmp/x.spinrec")
    db.end_capture_session("sess_1", end_reason="stopped")
    db.end_capture_session("sess_1", end_reason="crashed")  # second call is a no-op
    sess = db.get_capture_session("sess_1")
    assert sess["end_reason"] == "stopped"


def test_list_capture_sessions_for_run_orders_by_ordinal(db):
    db.create_capture_session("sess_a", "run_1", 2, "/tmp/a.spinrec")
    db.create_capture_session("sess_b", "run_1", 1, "/tmp/b.spinrec")
    db.create_capture_session("sess_c", "run_1", 3, "/tmp/c.spinrec")
    sessions = db.list_capture_sessions_for_run("run_1")
    assert [s["id"] for s in sessions] == ["sess_b", "sess_a", "sess_c"]
    assert [s["ordinal"] for s in sessions] == [1, 2, 3]


def test_mark_orphan_capture_sessions_crashed(db):
    # Two open sessions and one already-ended
    db.create_capture_session("sess_a", "run_1", 1, "/tmp/a.spinrec")
    db.end_capture_session("sess_a", end_reason="stopped")
    db.create_capture_session("sess_b", "run_1", 2, "/tmp/b.spinrec")
    db.create_capture_session("sess_c", "run_1", 3, "/tmp/c.spinrec")
    count = db.mark_orphan_capture_sessions_crashed("run_1")
    assert count == 2
    assert db.get_capture_session("sess_a")["end_reason"] == "stopped"
    assert db.get_capture_session("sess_b")["end_reason"] == "crashed"
    assert db.get_capture_session("sess_b")["ended_at"] is not None
    assert db.get_capture_session("sess_c")["end_reason"] == "crashed"


def test_max_session_ordinal_for_run(db):
    assert db.max_session_ordinal_for_run("run_1") == 0
    db.create_capture_session("sess_1", "run_1", 1, "/tmp/1.spinrec")
    db.create_capture_session("sess_2", "run_1", 2, "/tmp/2.spinrec")
    assert db.max_session_ordinal_for_run("run_1") == 2
