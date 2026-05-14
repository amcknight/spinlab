"""Recorded segment times — per-segment timing buffer.

Populated immediately as segments close during a recording session, drained
into `attempts` at finalize time. Enables crash-safe timing data without
polluting the `attempts` table with provisional rows.
"""
import sqlite3
from datetime import UTC, datetime
from typing import TypedDict


class RecordedSegmentTimeRow(TypedDict):
    id: int
    capture_session_id: str
    segment_id: str
    time_ms: int
    deaths: int
    clean_tail_ms: int
    recorded_at: str


class RecordedSegmentTimesMixin:
    """Per-segment timing buffer for in-progress reference runs.

    ``self.transaction()`` is the composable atomic-block CM from ``DatabaseCore``.
    """
    conn: sqlite3.Connection

    def add_recorded_segment_time(
        self, capture_session_id: str, segment_id: str,
        time_ms: int, deaths: int, clean_tail_ms: int,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "INSERT INTO recorded_segment_times "
            "(capture_session_id, segment_id, time_ms, deaths, clean_tail_ms, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (capture_session_id, segment_id, time_ms, deaths, clean_tail_ms, now),
        )

    def drain_recorded_segment_times_for_run(self, capture_run_id: str) -> list[RecordedSegmentTimeRow]:
        """Return all timing rows for the run, then delete them, atomically.

        SELECT and DELETE go in a single ``self.transaction()`` so a crash
        mid-drain cannot leave the buffer in a half-drained state that would
        seed duplicate attempts on retry. Composes under an outer transaction
        via SAVEPOINT.
        """
        with self.transaction():  # type: ignore[attr-defined]
            rows = self.conn.execute(
                "SELECT t.id, t.capture_session_id, t.segment_id, t.time_ms, "
                "t.deaths, t.clean_tail_ms, t.recorded_at "
                "FROM recorded_segment_times t "
                "JOIN capture_sessions s ON t.capture_session_id = s.id "
                "WHERE s.capture_run_id = ? ORDER BY t.id",
                (capture_run_id,),
            ).fetchall()
            result = [dict(r) for r in rows]
            ids = [r["id"] for r in result]
            if ids:
                placeholders = ",".join("?" * len(ids))
                self.conn.execute(
                    f"DELETE FROM recorded_segment_times WHERE id IN ({placeholders})", ids,
                )
        return result  # type: ignore[return-value]
