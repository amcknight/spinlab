"""Capture session (recording session within a capture run) queries.

Note on naming: `capture_sessions` are recording sessions within a multi-session
reference run. They are distinct from `sessions` (practice sessions) and from
`attempts.session_id` (polymorphic parent grouping). All three exist; read carefully.
"""
import sqlite3
from datetime import UTC, datetime
from typing import TypedDict


class CaptureSessionRow(TypedDict):
    id: str
    capture_run_id: str
    ordinal: int
    started_at: str
    ended_at: str | None
    spinrec_path: str
    end_reason: str | None


class CaptureSessionsMixin:
    """CRUD and recovery for capture_sessions."""
    conn: sqlite3.Connection

    def create_capture_session(
        self, session_id: str, capture_run_id: str,
        ordinal: int, spinrec_path: str,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "INSERT INTO capture_sessions "
            "(id, capture_run_id, ordinal, started_at, spinrec_path) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, capture_run_id, ordinal, now, spinrec_path),
        )
        self.conn.commit()

    def get_capture_session(self, session_id: str) -> CaptureSessionRow | None:
        row = self.conn.execute(
            "SELECT id, capture_run_id, ordinal, started_at, ended_at, "
            "spinrec_path, end_reason FROM capture_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        return dict(row)  # type: ignore[return-value]
