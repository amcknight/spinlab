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


def test_delete_capture_session_removes_row(db):
    db.create_capture_session("sess_1", "run_1", 1, "/tmp/x.spinrec")
    db.delete_capture_session("sess_1")
    assert db.get_capture_session("sess_1") is None


def test_delete_capture_session_cascades_to_recorded_segment_times(db):
    db.create_capture_session("sess_1", "run_1", 1, "/tmp/x.spinrec")
    db.add_recorded_segment_time("sess_1", "seg_x", time_ms=1000, deaths=0, clean_tail_ms=1000)
    rows = db.conn.execute(
        "SELECT COUNT(*) FROM recorded_segment_times WHERE capture_session_id = ?",
        ("sess_1",),
    ).fetchone()
    assert rows[0] == 1
    db.delete_capture_session("sess_1")
    rows = db.conn.execute(
        "SELECT COUNT(*) FROM recorded_segment_times WHERE capture_session_id = ?",
        ("sess_1",),
    ).fetchone()
    assert rows[0] == 0


def test_hard_delete_capture_run_cascades_to_sessions_and_times(db):
    db.create_capture_session("sess_1", "run_1", 1, "/tmp/1.spinrec")
    db.create_capture_session("sess_2", "run_1", 2, "/tmp/2.spinrec")
    db.add_recorded_segment_time("sess_1", "seg_a", time_ms=100, deaths=0, clean_tail_ms=100)
    db.hard_delete_capture_run("run_1")
    assert db.list_capture_sessions_for_run("run_1") == []
    rows = db.conn.execute("SELECT COUNT(*) FROM recorded_segment_times").fetchone()
    assert rows[0] == 0


def test_hard_delete_capture_run_removes_spinrec_files(tmp_path, db):
    spinrec_a = tmp_path / "a.spinrec"
    spinrec_b = tmp_path / "b.spinrec"
    spinrec_a.write_bytes(b"x")
    spinrec_b.write_bytes(b"y")
    db.create_capture_session("sess_1", "run_1", 1, str(spinrec_a))
    db.create_capture_session("sess_2", "run_1", 2, str(spinrec_b))
    db.hard_delete_capture_run("run_1")
    assert not spinrec_a.exists()
    assert not spinrec_b.exists()


def test_recover_paused_capture_run_finds_most_recent_draft(db):
    # Three draft runs for same game; recover picks most recent and removes all older ones
    import time
    # Bypass the unique-paused-run-per-game index to construct the multi-draft
    # scenario the recovery code defends against. Production code can't reach this
    # state (the index prevents it), but raw SQL can — and we still want to verify
    # recovery handles the edge case gracefully if it ever does.
    db.conn.execute("DROP INDEX IF EXISTS idx_one_paused_run_per_game")
    db.create_capture_run("run_old", "smw", "Old", draft=True)
    time.sleep(0.01)  # ensure different created_at
    db.create_capture_run("run_new", "smw", "New", draft=True)
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
    db.create_capture_session("sess_1", "run_1", 1, "/tmp/1.spinrec")
    # session_2 is open (orphan)
    db.create_capture_session("sess_2", "run_1", 2, "/tmp/2.spinrec")
    db.end_capture_session("sess_1", end_reason="stopped")
    db.recover_paused_capture_run("smw")
    sess_2 = db.get_capture_session("sess_2")
    assert sess_2["end_reason"] == "crashed"
