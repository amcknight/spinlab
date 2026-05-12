"""atomic_save_and_finish_run — atomic finalize of a recording capture_run.

Five mutations happen inside one BEGIN IMMEDIATE: end the capture session,
drain recorded_segment_times for the run, promote the draft to saved,
activate this run (deactivating sibling runs for the same game), and
insert seeded Attempt rows from the drained timing data. Either every
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
    timing rows). Raises whatever sqlite3 raises on mid-transaction failure;
    caller responsible for translating to ActionResult.
    """
    try:
        db.conn.execute("BEGIN IMMEDIATE")

        if session_id:
            db.conn.execute(
                "UPDATE capture_sessions SET ended_at = ?, end_reason = ? "
                "WHERE id = ? AND ended_at IS NULL",
                (_dt.now(UTC).isoformat(), "stopped", session_id),
            )

        rows = db.conn.execute(
            "SELECT t.id, t.capture_session_id, t.segment_id, t.time_ms, "
            "t.deaths, t.clean_tail_ms, t.recorded_at "
            "FROM recorded_segment_times t "
            "JOIN capture_sessions s ON t.capture_session_id = s.id "
            "WHERE s.capture_run_id = ? ORDER BY t.id",
            (run_id,),
        ).fetchall()
        timing_rows = [dict(r) for r in rows]
        ids = [r["id"] for r in timing_rows]
        if ids:
            placeholders = ",".join("?" * len(ids))
            db.conn.execute(
                f"DELETE FROM recorded_segment_times WHERE id IN ({placeholders})",
                ids,
            )

        db.conn.execute(
            "UPDATE capture_runs SET draft = 0, name = ? WHERE id = ?",
            (name, run_id),
        )

        game_row = db.conn.execute(
            "SELECT game_id FROM capture_runs WHERE id = ?", (run_id,),
        ).fetchone()
        if game_row:
            db.conn.execute(
                "UPDATE capture_runs SET active = 0 WHERE game_id = ?",
                (game_row[0],),
            )
            db.conn.execute(
                "UPDATE capture_runs SET active = 1 WHERE id = ?", (run_id,),
            )

        now = _dt.now(UTC)
        seeded: list[Attempt] = []
        for row in timing_rows:
            attempt = Attempt(
                segment_id=row["segment_id"],
                parent_id=run_id,
                completed=True,
                time_ms=row["time_ms"],
                deaths=row["deaths"],
                clean_tail_ms=row["clean_tail_ms"],
                source=AttemptSource.REFERENCE,
                created_at=now,
            )
            db.conn.execute(
                """INSERT INTO attempts
                   (segment_id, parent_id, completed, time_ms,
                    strat_version, source, deaths, clean_tail_ms,
                    observed_start_conditions, observed_end_conditions,
                    invalidated, chosen_allocator, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (attempt.segment_id, attempt.parent_id,
                 int(attempt.completed), attempt.time_ms,
                 attempt.strat_version, attempt.source,
                 attempt.deaths, attempt.clean_tail_ms,
                 attempt.observed_start_conditions,
                 attempt.observed_end_conditions,
                 int(attempt.invalidated),
                 attempt.chosen_allocator,
                 attempt.created_at.isoformat()),
            )
            seeded.append(attempt)

        db.conn.commit()
    except Exception:
        db.conn.rollback()
        raise

    return seeded
