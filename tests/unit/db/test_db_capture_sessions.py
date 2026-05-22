"""Tests for CaptureSessionsMixin."""
import pytest

from spinlab.db import Database


@pytest.fixture
def db():
    d = Database(":memory:")
    d.upsert_game("smw", "Super Mario World", "any%")
    d.create_capture_run("run_1", "smw", "Test Run", kind="live")
    yield d
    d.close()


def test_create_and_get_capture_session(db):
    db.create_capture_session(
        session_id="sess_1", capture_run_id="run_1",
        ordinal=1,
    )
    sess = db.get_capture_session("sess_1")
    assert sess is not None
    assert sess["id"] == "sess_1"
    assert sess["capture_run_id"] == "run_1"
    assert sess["ordinal"] == 1
    assert sess["started_at"] is not None
    assert sess["ended_at"] is None
    assert sess["end_reason"] is None


def test_get_capture_session_missing_returns_none(db):
    assert db.get_capture_session("nonexistent") is None


def test_end_capture_session_sets_ended_at_and_reason(db):
    db.create_capture_session("sess_1", "run_1", 1)
    db.end_capture_session("sess_1", end_reason="stopped")
    sess = db.get_capture_session("sess_1")
    assert sess["ended_at"] is not None
    assert sess["end_reason"] == "stopped"


def test_end_capture_session_is_idempotent(db):
    db.create_capture_session("sess_1", "run_1", 1)
    db.end_capture_session("sess_1", end_reason="stopped")
    db.end_capture_session("sess_1", end_reason="crashed")  # second call is a no-op
    sess = db.get_capture_session("sess_1")
    assert sess["end_reason"] == "stopped"


def test_list_capture_sessions_for_run_orders_by_ordinal(db):
    db.create_capture_session("sess_a", "run_1", 2)
    db.create_capture_session("sess_b", "run_1", 1)
    db.create_capture_session("sess_c", "run_1", 3)
    sessions = db.list_capture_sessions_for_run("run_1")
    assert [s["id"] for s in sessions] == ["sess_b", "sess_a", "sess_c"]
    assert [s["ordinal"] for s in sessions] == [1, 2, 3]


def test_mark_orphan_capture_sessions_crashed(db):
    # Two open sessions and one already-ended
    db.create_capture_session("sess_a", "run_1", 1)
    db.end_capture_session("sess_a", end_reason="stopped")
    db.create_capture_session("sess_b", "run_1", 2)
    db.create_capture_session("sess_c", "run_1", 3)
    count = db.mark_orphan_capture_sessions_crashed("run_1")
    assert count == 2
    assert db.get_capture_session("sess_a")["end_reason"] == "stopped"
    assert db.get_capture_session("sess_b")["end_reason"] == "crashed"
    assert db.get_capture_session("sess_b")["ended_at"] is not None
    assert db.get_capture_session("sess_c")["end_reason"] == "crashed"


def test_max_session_ordinal_for_run(db):
    assert db.max_session_ordinal_for_run("run_1") == 0
    db.create_capture_session("sess_1", "run_1", 1)
    db.create_capture_session("sess_2", "run_1", 2)
    assert db.max_session_ordinal_for_run("run_1") == 2


def test_delete_capture_session_removes_row(db):
    db.create_capture_session("sess_1", "run_1", 1)
    db.delete_capture_session("sess_1")
    assert db.get_capture_session("sess_1") is None




def test_recover_paused_capture_run_finds_most_recent_draft(db):
    # Three draft runs for same game; recover picks most recent and removes all older ones
    import time
    # Bypass the unique-paused-run-per-game index to construct the multi-draft
    # scenario the recovery code defends against. Production code can't reach this
    # state (the index prevents it), but raw SQL can — and we still want to verify
    # recovery handles the edge case gracefully if it ever does.
    db.conn.execute("DROP INDEX IF EXISTS idx_one_live_draft_per_game")
    db.create_capture_run("run_old", "smw", "Old", kind="live")
    time.sleep(0.01)  # ensure different created_at
    db.create_capture_run("run_new", "smw", "New", kind="live")
    found = db.recover_paused_capture_run("smw")
    assert found == "run_new"
    # All older drafts (run_1 from fixture and run_old) are gone; only run_new survives
    rows = db.conn.execute("SELECT id FROM capture_runs").fetchall()
    assert {r[0] for r in rows} == {"run_new"}


def test_recover_paused_capture_run_returns_none_when_no_drafts(db):
    # The fixture's run_1 is the only draft; remove it via finalize
    db.promote_draft("run_1", "Finalized")
    assert db.recover_paused_capture_run("smw") is None


def test_recover_paused_capture_run_marks_orphan_sessions_crashed(db):
    db.create_capture_session("sess_1", "run_1", 1)
    # session_2 is open (orphan)
    db.create_capture_session("sess_2", "run_1", 2)
    db.end_capture_session("sess_1", end_reason="stopped")
    db.recover_paused_capture_run("smw")
    sess_2 = db.get_capture_session("sess_2")
    assert sess_2["end_reason"] == "crashed"
