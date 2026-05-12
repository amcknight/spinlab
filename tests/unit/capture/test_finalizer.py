"""Tests for atomic_save_and_finish_run.

Task 1.1 writes the tests against the current inline implementation in
ReferenceController.save_and_finish_run — they verify the rollback contract
before extraction. Task 1.3 extracts the function; the same tests stay green.
"""
from __future__ import annotations

import pytest

from spinlab.models import Mode, Status


@pytest.mark.asyncio
async def test_rollback_on_mid_transaction_failure(
    reference_controller_recording, monkeypatch,
):
    """If a db.conn.execute call inside the atomic block raises, every prior
    mutation rolls back: draft stays 1, timing rows stay present, no attempts
    inserted."""
    ctl = reference_controller_recording
    db = ctl.db
    run_id = ctl.recorder.capture_run_id
    sess_id = ctl.recorder.current_capture_session_id
    assert run_id is not None
    assert sess_id is not None

    # Seed one timing row that would be drained
    db.conn.execute(
        "INSERT INTO recorded_segment_times "
        "(capture_session_id, segment_id, time_ms, deaths, clean_tail_ms, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, datetime('now'))",
        (sess_id, "seg1", 1000, 0, 1000),
    )
    db.conn.commit()

    # Wrap conn so execute raises on INSERT INTO attempts (last mutation in
    # the transaction). sqlite3.Connection.execute is C-level read-only, so
    # we monkeypatch db.conn itself to a proxy that filters by SQL.
    real_conn = db.conn

    class FailingConn:
        def execute(self, sql, *args, **kwargs):
            if "INSERT INTO attempts" in sql:
                raise RuntimeError("injected failure mid-transaction")
            return real_conn.execute(sql, *args, **kwargs)

        def commit(self):
            return real_conn.commit()

        def rollback(self):
            return real_conn.rollback()

    monkeypatch.setattr(db, "conn", FailingConn())

    with pytest.raises(RuntimeError, match="injected failure"):
        await ctl.save_and_finish_run(Mode.REFERENCE, "Test Name")

    monkeypatch.undo()  # restores db.conn to real_conn

    # capture_runs.draft still 1, name unchanged
    row = db.conn.execute(
        "SELECT draft, name FROM capture_runs WHERE id = ?", (run_id,),
    ).fetchone()
    assert row[0] == 1, "draft flag should have rolled back to 1"
    assert row[1] == "In-Progress", "name should not have been updated"

    # Timing row still present
    rows = db.conn.execute(
        "SELECT id FROM recorded_segment_times WHERE capture_session_id = ?",
        (sess_id,),
    ).fetchall()
    assert len(rows) == 1, "drained timing row should have been restored on rollback"

    # No attempts inserted for this run
    rows = db.conn.execute(
        "SELECT id FROM attempts WHERE parent_id = ?", (run_id,),
    ).fetchall()
    assert len(rows) == 0


@pytest.mark.asyncio
async def test_happy_path_commits_all_five_mutations(
    reference_controller_recording,
):
    """Happy path: session ended, timing rows drained, draft promoted,
    run activated, attempts seeded."""
    ctl = reference_controller_recording
    db = ctl.db
    run_id = ctl.recorder.capture_run_id
    sess_id = ctl.recorder.current_capture_session_id
    assert run_id is not None
    assert sess_id is not None

    db.conn.execute(
        "INSERT INTO recorded_segment_times "
        "(capture_session_id, segment_id, time_ms, deaths, clean_tail_ms, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, datetime('now'))",
        (sess_id, "seg1", 1234, 2, 800),
    )
    db.conn.commit()

    result = await ctl.save_and_finish_run(Mode.REFERENCE, "Finalized Name")

    assert result.status == Status.OK
    assert result.new_mode == Mode.IDLE

    sess_row = db.conn.execute(
        "SELECT ended_at FROM capture_sessions WHERE id = ?", (sess_id,),
    ).fetchone()
    assert sess_row[0] is not None

    rows = db.conn.execute(
        "SELECT id FROM recorded_segment_times WHERE capture_session_id = ?",
        (sess_id,),
    ).fetchall()
    assert rows == []

    cap = db.conn.execute(
        "SELECT draft, name, active FROM capture_runs WHERE id = ?", (run_id,),
    ).fetchone()
    assert cap[0] == 0, "draft should be promoted to 0"
    assert cap[1] == "Finalized Name"
    assert cap[2] == 1, "run should be activated"

    att = db.conn.execute(
        "SELECT segment_id, time_ms, deaths, source FROM attempts "
        "WHERE parent_id = ?", (run_id,),
    ).fetchone()
    assert att is not None
    assert att[0] == "seg1"
    assert att[1] == 1234
    assert att[2] == 2
    assert att[3] == "reference"
