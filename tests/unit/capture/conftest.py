"""Fixtures for capture-package unit tests."""
from __future__ import annotations

import uuid

import pytest
from tests.conftest import FakeEmuBackend, make_seg_with_state

from spinlab.capture.reference import ReferenceController
from spinlab.db import Database


@pytest.fixture
def reference_controller_recording(tmp_path):
    """A ReferenceController in RECORDING state with capture_run + session rows
    and one segment (id="seg1") so FK-bound inserts on attempts succeed.

    Bypasses ``start_reference`` (which is async) by writing the run/session
    rows directly and calling ``_enter_recording``. Wired to a real sqlite
    Database and a connected FakeEmuBackend.
    """
    db = Database(tmp_path / "ref.db")
    db.upsert_game("g1", "Test Game", "any%")
    state_file = tmp_path / "seg1_hot.mss"
    state_file.write_bytes(b"")
    seg = make_seg_with_state(db, "g1", 1, "entrance", "goal", state_file)
    # Tests use "seg1" as the timing row's segment_id; rewrite the inserted
    # segment id to "seg1" so the FK on attempts.segment_id is satisfied.
    db.conn.execute("UPDATE segments SET id = 'seg1' WHERE id = ?", (seg.id,))
    db.conn.commit()

    emu = FakeEmuBackend(connected=True)
    ctl = ReferenceController(db, emu)

    run_id = f"live_{uuid.uuid4().hex[:8]}"
    db.create_capture_run(run_id, "g1", "In-Progress", draft=True)
    sess_id = f"sess_{uuid.uuid4().hex[:8]}"
    db.create_capture_session(session_id=sess_id, capture_run_id=run_id, ordinal=1)
    ctl._enter_recording(run_id, sess_id)
    return ctl
