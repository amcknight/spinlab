"""atomic_save_and_finish_run — atomic finalize of a recording capture_run.

Three mutations happen inside one ``db.transaction()``: end the capture
session, promote the draft to saved, activate this run (deactivating
sibling runs for the same game). Either every step succeeds and commits,
or any failure rolls back and re-raises.

Event rows for the captured segments were already written by the
SegmentRecorder as each segment closed; finalize does not touch
`attempts` at all. The pre-2026-05 drain-and-seed step is gone with
the `recorded_segment_times` table.

Caller is responsible for the recorder-state transition to idle and
any scheduler.rebuild_all_states() call — those are not part of the
atomic unit.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from spinlab.db import Database

logger = logging.getLogger(__name__)


def atomic_save_and_finish_run(
    db: "Database",
    run_id: str,
    session_id: str | None,
    name: str,
) -> None:
    """End session + promote draft + activate. Atomic; any failure rolls back."""
    with db.transaction():
        if session_id:
            db.end_capture_session(session_id, end_reason="stopped")
        db.promote_draft(run_id, name)
        db.set_active_capture_run(run_id)
