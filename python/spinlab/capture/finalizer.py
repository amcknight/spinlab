"""atomic_save_and_finish_run — atomic finalize of a recording capture_run.

Five mutations happen inside one ``db.transaction()``: end the capture
session, drain recorded_segment_times for the run, promote the draft to
saved, activate this run (deactivating sibling runs for the same game),
and insert seeded Attempt rows from the drained timing data. Either every
step succeeds and commits, or any failure rolls back and re-raises.

Caller is responsible for the recorder-state transition to idle and any
scheduler.rebuild_all_states() call — those are not part of the atomic
unit.
"""
from __future__ import annotations

import logging
from datetime import UTC
from datetime import datetime as _dt
from typing import TYPE_CHECKING

from spinlab.models import Attempt, AttemptSource

if TYPE_CHECKING:
    from spinlab.db import Database

logger = logging.getLogger(__name__)


def atomic_save_and_finish_run(
    db: "Database",
    run_id: str,
    session_id: str | None,
    name: str,
) -> list[Attempt]:
    """End session + drain timing rows + promote draft + activate + seed attempts.

    Returns the seeded Attempt objects (empty list if there were no drained
    timing rows). Any failure raises and rolls back all mutations atomically.
    """
    seeded: list[Attempt] = []
    with db.transaction():
        if session_id:
            db.end_capture_session(session_id, end_reason="stopped")
        timing_rows = db.drain_recorded_segment_times_for_run(run_id)
        db.promote_draft(run_id, name)
        db.set_active_capture_run(run_id)
        now = _dt.now(UTC)
        for row in timing_rows:
            attempt = Attempt(
                segment_id=row["segment_id"],
                capture_run_id=run_id,
                completed=True,
                time_ms=row["time_ms"],
                deaths=row["deaths"],
                clean_tail_ms=row["clean_tail_ms"],
                source=AttemptSource.REFERENCE,
                created_at=now,
            )
            db.log_attempt(attempt)
            seeded.append(attempt)
    return seeded
