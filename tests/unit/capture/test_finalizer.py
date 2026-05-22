"""Tests for atomic_save_and_finish_run.

The finalize path no longer drains recorded_segment_times or seeds
attempts — those rows were already written event-by-event by the
recorder. Finalize is now: end the capture_session, promote the
draft to saved, activate. These tests pin that contract.
"""
from __future__ import annotations

import pytest

from spinlab.models import Mode, Status


@pytest.mark.asyncio
async def test_happy_path_promotes_and_activates(reference_controller_recording):
    """Happy path: session ended, draft promoted to saved, run activated.
    No row movement (event rows already exist from the recorder)."""
    ctl = reference_controller_recording
    db = ctl.db
    run_id = ctl.recorder.capture_run_id
    sess_id = ctl.recorder.current_capture_session_id
    assert run_id is not None
    assert sess_id is not None

    result = await ctl.save_and_finish_run(Mode.REFERENCE, "Finalized Name")

    assert result.status == Status.OK
    assert result.new_mode == Mode.IDLE

    sess_row = db.conn.execute(
        "SELECT ended_at FROM capture_sessions WHERE id = ?", (sess_id,),
    ).fetchone()
    assert sess_row[0] is not None, "capture_session should be ended"

    cap = db.conn.execute(
        "SELECT status, name, active FROM capture_runs WHERE id = ?", (run_id,),
    ).fetchone()
    assert cap[0] == "saved", "status promoted to saved"
    assert cap[1] == "Finalized Name"
    assert cap[2] == 1, "run activated"


@pytest.mark.asyncio
async def test_rollback_on_mid_transaction_failure(
    reference_controller_recording, monkeypatch,
):
    """If any mutation in the finalize transaction raises, every prior
    mutation rolls back: draft stays 1, name unchanged, capture_session
    not ended."""
    ctl = reference_controller_recording
    db = ctl.db
    run_id = ctl.recorder.capture_run_id
    sess_id = ctl.recorder.current_capture_session_id
    assert run_id is not None
    assert sess_id is not None

    real_conn = db.conn

    class FailingConn:
        def execute(self, sql, *args, **kwargs):
            # Fail on the activation step (UPDATE capture_runs SET active=1).
            # This is the last mutation in the transaction; failing here
            # exercises the full rollback of session-end + promote-draft.
            if "SET active = 1" in sql or "SET active=1" in sql:
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

    monkeypatch.undo()

    row = db.conn.execute(
        "SELECT status, name FROM capture_runs WHERE id = ?", (run_id,),
    ).fetchone()
    assert row[0] == "draft", "status rolled back to draft"
    assert row[1] == "In-Progress", "name unchanged"

    sess_row = db.conn.execute(
        "SELECT ended_at FROM capture_sessions WHERE id = ?", (sess_id,),
    ).fetchone()
    assert sess_row[0] is None, "capture_session end rolled back"


@pytest.mark.asyncio
async def test_finalize_run_rollback_on_mid_transaction_failure(
    reference_controller_recording, monkeypatch,
):
    """Same atomicity guarantee on the paused-run path. Stop the
    recording first to enter PAUSED, then injecting a failure on the
    activation step rolls back the promote_draft too — draft stays draft.
    """
    ctl = reference_controller_recording
    db = ctl.db
    run_id = ctl.recorder.capture_run_id
    assert run_id is not None

    # Drop into PAUSED state (stop_reference ends the session and surfaces
    # the run as paused_run_id).
    await ctl.stop_reference(Mode.REFERENCE)
    assert ctl.paused_run_id == run_id

    real_conn = db.conn

    class FailingConn:
        def execute(self, sql, *args, **kwargs):
            if "SET active = 1" in sql or "SET active=1" in sql:
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
        await ctl.finalize_run("Test Name")

    monkeypatch.undo()

    row = db.conn.execute(
        "SELECT status, name FROM capture_runs WHERE id = ?", (run_id,),
    ).fetchone()
    assert row[0] == "draft", "status rolled back to draft (promote was atomic with activate)"
    assert row[1] == "In-Progress", "name unchanged"
