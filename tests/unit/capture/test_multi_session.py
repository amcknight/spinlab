"""Multi-session reference run lifecycle tests."""
import logging
import re
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from tests.conftest import FakeEmuBackend

from spinlab.capture import ReferenceController
from spinlab.db import Database
from spinlab.errors import DraftPendingError, SessionDeleteAfterFinalizeError
from spinlab.models import Mode, Status


@pytest.fixture
def db(tmp_path):
    d = Database(":memory:")
    d.upsert_game("smw", "Super Mario World", "any%")
    yield d
    d.close()


@pytest.fixture
def emu():
    return FakeEmuBackend(connected=True)


@pytest.fixture
def controller(db, emu):
    return ReferenceController(db, emu)


@pytest_asyncio.fixture
async def started_session(controller, db, tmp_path):
    """A controller in RECORDING with an open session under a fresh run."""
    result = await controller.start_reference(
        Mode.IDLE, "smw", tmp_path, run_name="Test Run",
    )
    assert result.new_mode == Mode.REFERENCE
    return controller


# --- Save & Finish from already-stopped (paused) state ---

@pytest.mark.asyncio
async def test_save_and_finish_from_paused_after_stop_finalizes(started_session, db):
    """Regression: clicking Save & Finish AFTER Stop should finalize the
    paused run, not silently 409. The dashboard's primary save button stays
    visible after Stop and users expect it to work either way."""
    sess_id = started_session.recorder.current_capture_session_id
    run_id = started_session.recorder.capture_run_id
    db.add_recorded_segment_time(sess_id, "seg_a", time_ms=2000, deaths=0, clean_tail_ms=2000)
    _make_minimal_segment(db, run_id, sess_id, "seg_a")

    # Stop first (mode goes REFERENCE → IDLE, run becomes paused).
    await started_session.stop_reference(Mode.REFERENCE)
    assert started_session.has_paused_run

    # Now Save & Finish from IDLE should finalize the paused run.
    result = await started_session.save_and_finish_run(Mode.IDLE, name="Stopped First")
    assert result.status == Status.OK
    row = db.conn.execute(
        "SELECT status, name FROM capture_runs WHERE id = ?", (run_id,)
    ).fetchone()
    assert row[0] == "saved"  # promoted from draft
    assert row[1] == "Stopped First"


# --- Single-session save_and_finish path ---

@pytest.mark.asyncio
async def test_save_and_finish_seeds_attempts_and_finalizes(started_session, db):
    sess_id = started_session.recorder.current_capture_session_id
    run_id = started_session.recorder.capture_run_id
    db.add_recorded_segment_time(sess_id, "seg_x", time_ms=1500, deaths=0, clean_tail_ms=1500)
    _make_minimal_segment(db, run_id, sess_id, "seg_x")

    result = await started_session.save_and_finish_run(Mode.REFERENCE, name="My Run")

    assert result.status == Status.OK
    assert result.new_mode == Mode.IDLE
    row = db.conn.execute("SELECT status, name FROM capture_runs WHERE id = ?", (run_id,)).fetchone()
    assert row[0] == "saved"
    assert row[1] == "My Run"
    attempts = db.conn.execute(
        "SELECT segment_id, time_ms FROM attempts WHERE segment_id = 'seg_x'"
    ).fetchall()
    assert [(r[0], r[1]) for r in attempts] == [("seg_x", 1500)]
    rows = db.conn.execute(
        "SELECT COUNT(*) FROM recorded_segment_times "
        "WHERE capture_session_id = ?", (sess_id,)
    ).fetchone()
    assert rows[0] == 0


# --- Multi-session: stop then resume ---

@pytest.mark.asyncio
async def test_stop_session_pauses_run(started_session, db):
    run_id = started_session.recorder.capture_run_id
    result = await started_session.stop_reference(Mode.REFERENCE)
    assert result.new_mode == Mode.IDLE
    assert started_session.paused_run_id == run_id
    sessions = db.list_capture_sessions_for_run(run_id)
    assert len(sessions) == 1
    assert sessions[0]["end_reason"] == "stopped"
    status = db.conn.execute(
        "SELECT status FROM capture_runs WHERE id = ?", (run_id,)
    ).fetchone()[0]
    assert status == "draft"


@pytest.mark.asyncio
async def test_resume_creates_new_session_under_same_run(started_session, db, tmp_path):
    run_id = started_session.recorder.capture_run_id
    await started_session.stop_reference(Mode.REFERENCE)

    result = await started_session.resume_reference(Mode.IDLE, "smw", tmp_path)
    assert result.new_mode == Mode.REFERENCE
    assert started_session.recorder.capture_run_id == run_id
    assert started_session.paused_run_id is None
    sessions = db.list_capture_sessions_for_run(run_id)
    assert [s["ordinal"] for s in sessions] == [1, 2]
    assert sessions[1]["ended_at"] is None


# --- Discard ---

@pytest.mark.asyncio
async def test_discard_run_hard_deletes_everything(started_session, db):
    run_id = started_session.recorder.capture_run_id
    sess_id = started_session.recorder.current_capture_session_id
    db.add_recorded_segment_time(sess_id, "seg_x", time_ms=100, deaths=0, clean_tail_ms=100)
    await started_session.stop_reference(Mode.REFERENCE)

    result = await started_session.discard_run()
    assert result.status == Status.OK
    assert started_session.paused_run_id is None
    assert db.list_capture_sessions_for_run(run_id) == []
    rows = db.conn.execute("SELECT COUNT(*) FROM capture_runs WHERE id = ?", (run_id,)).fetchone()
    assert rows[0] == 0


# --- Delete session ---

@pytest.mark.asyncio
async def test_delete_capture_session_while_paused(started_session, db, tmp_path):
    run_id = started_session.recorder.capture_run_id
    sess_id = started_session.recorder.current_capture_session_id
    await started_session.stop_reference(Mode.REFERENCE)
    await started_session.resume_reference(Mode.IDLE, "smw", tmp_path)
    sess_2 = started_session.recorder.current_capture_session_id
    await started_session.stop_reference(Mode.REFERENCE)
    result = await started_session.delete_capture_session(sess_id)
    assert result.status == Status.OK
    sessions = db.list_capture_sessions_for_run(run_id)
    assert {s["id"] for s in sessions} == {sess_2}


@pytest.mark.asyncio
async def test_delete_capture_session_after_finalize_rejected(started_session, db):
    sess_id = started_session.recorder.current_capture_session_id
    await started_session.save_and_finish_run(Mode.REFERENCE, name="Done")
    with pytest.raises(SessionDeleteAfterFinalizeError):
        await started_session.delete_capture_session(sess_id)


# --- One paused run per game ---

@pytest.mark.asyncio
async def test_start_reference_rejects_when_paused_run_exists(started_session, tmp_path):
    await started_session.stop_reference(Mode.REFERENCE)
    with pytest.raises(DraftPendingError):
        await started_session.start_reference(
            Mode.IDLE, "smw", tmp_path, run_name="Other",
        )


# --- Disconnect ---

@pytest.mark.asyncio
async def test_disconnect_pauses_run(started_session, db):
    run_id = started_session.recorder.capture_run_id
    started_session.handle_disconnect()
    assert started_session.paused_run_id == run_id
    sessions = db.list_capture_sessions_for_run(run_id)
    assert sessions[0]["end_reason"] == "disconnected"


# --- Atomicity of save_and_finish_run ---

@pytest.mark.asyncio
async def test_save_and_finish_is_atomic_rolls_back_on_failure(started_session, db):
    """save_and_finish_run rolls back if attempt seeding raises a DB error.

    We inject a fault by adding a timing row that references a non-existent
    segment_id. With PRAGMA foreign_keys=ON, the INSERT INTO attempts for that
    row raises IntegrityError. The run must remain draft=1 and no attempts or
    timing-row deletes must survive.

    Choosing option B (explicit BEGIN IMMEDIATE with raw SQL) means all
    mutations — drain, promote, set_active, seed — happen in one transaction.
    A partial failure must leave the DB as if nothing happened.
    """
    import sqlite3

    sess_id = started_session.recorder.current_capture_session_id
    run_id = started_session.recorder.capture_run_id
    # One valid segment + one timing row pointing at a non-existent segment
    db.add_recorded_segment_time(sess_id, "seg_a", time_ms=1000, deaths=0, clean_tail_ms=1000)
    _make_minimal_segment(db, run_id, sess_id, "seg_a")
    # This timing row references "seg_ghost" which has no segments row → FK error
    db.add_recorded_segment_time(sess_id, "seg_ghost", time_ms=2000, deaths=0, clean_tail_ms=2000)

    with pytest.raises(sqlite3.IntegrityError):
        await started_session.save_and_finish_run(Mode.REFERENCE, name="Should Roll Back")

    # Run must still be draft (promotion was rolled back)
    row = db.conn.execute("SELECT status FROM capture_runs WHERE id = ?", (run_id,)).fetchone()
    assert row is not None and row[0] == "draft", "run must remain draft after rollback"
    # No attempts persisted
    attempt_count = db.conn.execute(
        "SELECT COUNT(*) FROM attempts WHERE capture_run_id = ?", (run_id,)
    ).fetchone()[0]
    assert attempt_count == 0, "no attempts should survive a rolled-back transaction"
    # Timing rows must NOT have been deleted (drain was rolled back too)
    timing_count = db.conn.execute(
        "SELECT COUNT(*) FROM recorded_segment_times WHERE capture_session_id = ?",
        (sess_id,),
    ).fetchone()[0]
    assert timing_count == 2, "timing rows must be intact after rollback"


def test_recovery_logs_warning_when_discarding_stranded_drafts(db, caplog):
    """Two paused drafts for the same game — recovery keeps the newest and warns
    about the discarded one. No silent data loss."""
    db.upsert_game("smw", "SMW", "any%")
    older = "older_run"
    newer = "newer_run"
    # Bypass the unique-paused-run-per-game index to construct the stranded-drafts
    # scenario the recovery code defends against. Production code can't reach this
    # state (the index prevents it), but raw SQL can — and we still want to verify
    # recovery handles the edge case gracefully if it ever does.
    db.conn.execute("DROP INDEX IF EXISTS idx_one_live_draft_per_game")
    db.create_capture_run(older, "smw", "Older", kind="live")
    db.create_capture_run(newer, "smw", "Newer", kind="live")
    # Force created_at ordering
    db.conn.execute(
        "UPDATE capture_runs SET created_at = ? WHERE id = ?",
        ((datetime.now(UTC) - timedelta(hours=1)).isoformat(), older),
    )
    db.conn.commit()

    with caplog.at_level(logging.WARNING, logger="spinlab.db.capture_sessions"):
        recovered = db.recover_paused_capture_run("smw")

    assert recovered == newer
    discard_warnings = [r for r in caplog.records if "discarding stranded draft" in r.getMessage().lower()]
    assert len(discard_warnings) == 1
    assert older in discard_warnings[0].getMessage()


def test_session_end_log_includes_ordinal_duration_segments(db, caplog):
    """When a capture session ends, log line includes ordinal, duration, and
    segment count to aid post-hoc debugging."""
    db.upsert_game("smw", "SMW", "any%")
    db.create_capture_run("run_x", "smw", "X", kind="live")
    db.create_capture_session("sess_x", "run_x", 3)
    # Add one segment so segment count > 0
    db.conn.execute(
        "INSERT INTO segments (id, game_id, level_number, start_type, start_ordinal, "
        "end_type, end_ordinal, capture_session_id, capture_run_id, created_at, updated_at)"
        "VALUES ('seg1', 'smw', 1, 'entrance', 0, 'goal', 0, 'sess_x', 'run_x', "
        "datetime('now'), datetime('now'))"
    )
    db.conn.commit()

    ctl = ReferenceController(db, FakeEmuBackend(connected=False))
    ctl.recorder.capture_run_id = "run_x"
    ctl.recorder.current_capture_session_id = "sess_x"

    with caplog.at_level(logging.INFO, logger="spinlab.capture.reference"):
        ctl._end_current_session(end_reason="stopped")

    msgs = [r.getMessage() for r in caplog.records]
    end_msgs = [m for m in msgs if "session: ended" in m.lower()]
    assert end_msgs, f"no session-end log; got: {msgs}"
    msg = end_msgs[0]
    # Assert intent (the ordinal, duration, and segment count are present in some form)
    # rather than exact substring layout — robust to log-format refactors.
    assert re.search(r"\bordinal\b.*\b3\b", msg), msg
    assert re.search(r"\bsegments?\b.*\b1\b", msg), msg
    assert "stopped" in msg
    assert re.search(r"\bduration", msg.lower()), msg


def test_list_capture_sessions_includes_segment_count(db):
    db.upsert_game("smw", "SMW", "any%")
    db.create_capture_run("run_y", "smw", "Y", kind="live")
    db.create_capture_session("s1", "run_y", 1)
    db.create_capture_session("s2", "run_y", 2)
    # 2 segments in s1, 1 in s2
    for sid, csid in [("a", "s1"), ("b", "s1"), ("c", "s2")]:
        db.conn.execute(
            "INSERT INTO segments (id, game_id, level_number, start_type, "
            "start_ordinal, end_type, end_ordinal, capture_session_id, "
            "capture_run_id, created_at, updated_at)VALUES (?, 'smw', 1, "
            "'entrance', 0, 'goal', 0, ?, 'run_y', datetime('now'), datetime('now'))",
            (sid, csid),
        )
    db.conn.commit()

    sessions = db.list_capture_sessions_for_run("run_y")
    counts = {s["id"]: s["segment_count"] for s in sessions}
    assert counts == {"s1": 2, "s2": 1}


def test_get_segments_by_reference_includes_session_ordinal(db):
    db.upsert_game("smw", "SMW", "any%")
    db.create_capture_run("run_z", "smw", "Z", kind="live")
    db.create_capture_session("s1", "run_z", 1)
    db.create_capture_session("s2", "run_z", 2)
    db.conn.execute(
        "INSERT INTO segments (id, game_id, level_number, start_type, "
        "start_ordinal, end_type, end_ordinal, capture_session_id, "
        "capture_run_id, created_at, updated_at)VALUES "
        "('a', 'smw', 1, 'entrance', 0, 'goal', 0, 's1', 'run_z', "
        "datetime('now'), datetime('now')), "
        "('b', 'smw', 1, 'entrance', 0, 'goal', 0, 's2', 'run_z', "
        "datetime('now'), datetime('now')), "
        "('c', 'smw', 1, 'entrance', 0, 'goal', 0, NULL, 'run_z', "
        "datetime('now'), datetime('now'))"
    )
    db.conn.commit()
    segs = db.get_segments_by_reference("run_z")
    by_id = {s["id"]: s for s in segs}
    assert by_id["a"]["session_ordinal"] == 1
    assert by_id["b"]["session_ordinal"] == 2
    assert by_id["c"]["session_ordinal"] is None


def test_finalize_rebuilds_scheduler_even_when_zero_segments(db):
    """Activating a reference invalidates scheduler state regardless of how many
    new attempts were seeded. Rebuild must fire."""
    import asyncio

    from tests.conftest import FakeEmuBackend

    from spinlab.capture.reference import ReferenceController

    db.upsert_game("smw", "SMW", "any%")
    db.create_capture_run("run_e", "smw", "Empty", kind="live")
    db.create_capture_session("s_e", "run_e", 1)

    class RecordingScheduler:
        def __init__(self): self.rebuild_calls = 0
        def rebuild_all_states(self): self.rebuild_calls += 1
    sched = RecordingScheduler()

    ctl = ReferenceController(db, FakeEmuBackend(connected=False))
    ctl.paused_run_id = "run_e"

    asyncio.run(ctl.finalize_run(name="Empty Run", scheduler=sched))

    assert sched.rebuild_calls == 1, (
        "scheduler must rebuild after set_active_capture_run, even with zero seeded attempts"
    )


def test_finalize_raises_no_paused_run_error_when_no_run(db):
    import asyncio

    from tests.conftest import FakeEmuBackend

    from spinlab.capture.reference import ReferenceController
    from spinlab.errors import NoPausedRunError
    ctl = ReferenceController(db, FakeEmuBackend(connected=False))
    ctl.paused_run_id = None
    with pytest.raises(NoPausedRunError):
        asyncio.run(ctl.finalize_run(name="x", scheduler=None))


def test_two_paused_drafts_for_same_game_violate_unique_index(db):
    """Belt-and-suspenders constraint: at most one non-replay draft per game."""
    import sqlite3
    db.upsert_game("smw", "SMW", "any%")
    db.create_capture_run("run_1", "smw", "1", kind="live")
    with pytest.raises(sqlite3.IntegrityError):
        db.create_capture_run("run_2", "smw", "2", kind="live")


def test_replay_drafts_can_coexist_with_paused_run(db):
    """The unique index filters on kind='live', so a replay-kind draft does NOT
    collide with a real live-kind paused run."""
    db.upsert_game("smw", "SMW", "any%")
    db.create_capture_run("run_real", "smw", "Real", kind="live")
    # Should not raise:
    db.create_capture_run("replay_xx", "smw", "Replay", kind="replay")


def test_delete_active_capture_session_raises_session_in_use(db):
    """If the recorder is currently writing into the session, deletion must
    raise SessionInUseError instead of leaving a dangling FK."""
    import asyncio

    from tests.conftest import FakeEmuBackend

    from spinlab.capture.reference import ReferenceController
    from spinlab.errors import SessionInUseError
    db.upsert_game("smw", "SMW", "any%")
    db.create_capture_run("run_d", "smw", "D", kind="live")
    db.create_capture_session("active_sess", "run_d", 1)

    ctl = ReferenceController(db, FakeEmuBackend(connected=False))
    ctl.recorder.capture_run_id = "run_d"
    ctl.recorder.current_capture_session_id = "active_sess"

    with pytest.raises(SessionInUseError):
        asyncio.run(ctl.delete_capture_session("active_sess"))


# --- Helpers ---

def _make_minimal_segment(db, run_id, sess_id, seg_id):
    """Insert a minimal valid segment row for FK referential integrity."""
    from spinlab.models import EndpointType, Segment, Waypoint
    wp_a = Waypoint.make("smw", 1, EndpointType.ENTRANCE, 0, {})
    wp_b = Waypoint.make("smw", 1, EndpointType.GOAL, 0, {})
    db.upsert_waypoint(wp_a)
    db.upsert_waypoint(wp_b)
    seg = Segment(
        id=seg_id, game_id="smw", level_number=1,
        start_type=EndpointType.ENTRANCE, start_ordinal=0,
        end_type=EndpointType.GOAL, end_ordinal=0,
        start_waypoint_id=wp_a.id, end_waypoint_id=wp_b.id,
        capture_run_id=run_id, capture_session_id=sess_id,
    )
    db.upsert_segment(seg)



