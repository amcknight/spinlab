"""Tests for atomic_save_and_finish_run.

Post-2026-05 refactor: attempt rows are written by SegmentRecorder at segment
close, not drained from recorded_segment_times at finalize. The
atomic_save_and_finish_run function now does exactly three mutations:
  1. end_capture_session
  2. promote_draft → 'saved'
  3. set_active_capture_run

These tests verify the rollback contract and the happy path for that
three-mutation atomic block.
"""
from __future__ import annotations

import pytest

from spinlab.models import Mode, Status


@pytest.mark.asyncio
async def test_rollback_on_mid_transaction_failure(
    reference_controller_recording, monkeypatch,
):
    """If a db.conn.execute call inside the atomic block raises, every prior
    mutation rolls back: session end_at is not committed, draft status stays
    'draft', run does not become active."""
    ctl = reference_controller_recording
    db = ctl.db
    run_id = ctl.recorder.capture_run_id
    sess_id = ctl.recorder.current_capture_session_id
    assert run_id is not None
    assert sess_id is not None

    # Wrap conn so execute raises on UPDATE capture_runs SET active=1 (the
    # last mutation inside atomic_save_and_finish_run). sqlite3.Connection.execute
    # is C-level read-only, so we monkeypatch db.conn itself to a proxy.
    real_conn = db.conn

    class FailingConn:
        def execute(self, sql, *args, **kwargs):
            if "SET active = 1" in sql:
                raise RuntimeError("injected failure mid-transaction")
            return real_conn.execute(sql, *args, **kwargs)

        def commit(self):
            return real_conn.commit()

        def rollback(self):
            return real_conn.rollback()

        @property
        def in_transaction(self):
            return real_conn.in_transaction

    monkeypatch.setattr(db, "conn", FailingConn())

    with pytest.raises(RuntimeError, match="injected failure"):
        await ctl.save_and_finish_run(Mode.REFERENCE, "Test Name")

    monkeypatch.undo()  # restores db.conn to real_conn

    # capture_runs.status still draft, name unchanged
    row = db.conn.execute(
        "SELECT status, name FROM capture_runs WHERE id = ?", (run_id,),
    ).fetchone()
    assert row[0] == "draft", "status should have rolled back to draft"
    assert row[1] == "In-Progress", "name should not have been updated"

    # Session must not have been ended (rolled back)
    sess_row = db.conn.execute(
        "SELECT ended_at FROM capture_sessions WHERE id = ?", (sess_id,),
    ).fetchone()
    assert sess_row[0] is None, "session end should have been rolled back"


@pytest.mark.asyncio
async def test_happy_path_commits_all_three_mutations(
    reference_controller_recording,
):
    """Happy path: session ended, draft promoted, run activated.

    Attempt rows are written by SegmentRecorder at segment close (not here),
    so we verify finalize commits the lifecycle mutations without touching
    attempts at all.
    """
    ctl = reference_controller_recording
    db = ctl.db
    run_id = ctl.recorder.capture_run_id
    sess_id = ctl.recorder.current_capture_session_id
    assert run_id is not None
    assert sess_id is not None

    result = await ctl.save_and_finish_run(Mode.REFERENCE, "Finalized Name")

    assert result.status == Status.OK
    assert result.new_mode == Mode.IDLE

    # 1. Session was ended
    sess_row = db.conn.execute(
        "SELECT ended_at FROM capture_sessions WHERE id = ?", (sess_id,),
    ).fetchone()
    assert sess_row[0] is not None, "session should have been ended"

    # 2. Draft promoted and named
    cap = db.conn.execute(
        "SELECT status, name, active FROM capture_runs WHERE id = ?", (run_id,),
    ).fetchone()
    assert cap[0] == "saved", "status should be promoted to saved"
    assert cap[1] == "Finalized Name"
    assert cap[2] == 1, "run should be activated"
