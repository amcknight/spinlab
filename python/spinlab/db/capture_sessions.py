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

    def end_capture_session(self, session_id: str, end_reason: str) -> None:
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "UPDATE capture_sessions SET ended_at = ?, end_reason = ? "
            "WHERE id = ? AND ended_at IS NULL",
            (now, end_reason, session_id),
        )
        self.conn.commit()

    def list_capture_sessions_for_run(self, capture_run_id: str) -> list[CaptureSessionRow]:
        rows = self.conn.execute(
            "SELECT id, capture_run_id, ordinal, started_at, ended_at, "
            "spinrec_path, end_reason FROM capture_sessions "
            "WHERE capture_run_id = ? ORDER BY ordinal",
            (capture_run_id,),
        ).fetchall()
        return [dict(r) for r in rows]  # type: ignore[return-value]

    def mark_orphan_capture_sessions_crashed(self, capture_run_id: str) -> int:
        """Mark any open sessions (ended_at IS NULL) as crashed. Returns count updated."""
        now = datetime.now(UTC).isoformat()
        cur = self.conn.execute(
            "UPDATE capture_sessions SET ended_at = ?, end_reason = 'crashed' "
            "WHERE capture_run_id = ? AND ended_at IS NULL",
            (now, capture_run_id),
        )
        self.conn.commit()
        return cur.rowcount

    def max_session_ordinal_for_run(self, capture_run_id: str) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(ordinal), 0) FROM capture_sessions "
            "WHERE capture_run_id = ?",
            (capture_run_id,),
        ).fetchone()
        return int(row[0])

    def delete_capture_session(self, session_id: str) -> None:
        """Delete a capture session. FK ON DELETE CASCADE removes related rows."""
        self.conn.execute(
            "DELETE FROM capture_sessions WHERE id = ?", (session_id,),
        )
        self.conn.commit()

    def recover_paused_capture_run(self, game_id: str) -> str | None:
        """Find the most recent draft (paused) capture_run for the game.

        Side effects:
        - Hard-deletes any older drafts for the same game (defensive — there
          should only be one paused run per game; if there are more, the
          oldest were stranded and are not recoverable into a coherent state).
        - Marks any orphaned open sessions for the recovered run as crashed.

        Returns the recovered run id, or None if no draft exists.
        """
        rows = self.conn.execute(
            "SELECT id FROM capture_runs WHERE game_id = ? AND draft = 1 "
            "ORDER BY created_at DESC",
            (game_id,),
        ).fetchall()
        if not rows:
            return None
        recovered_id = rows[0][0]
        for row in rows[1:]:
            self.hard_delete_capture_run(row[0])
        self.mark_orphan_capture_sessions_crashed(recovered_id)
        return recovered_id
