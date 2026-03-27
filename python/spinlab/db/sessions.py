"""Session queries."""

from datetime import UTC, datetime
from typing import Optional


class SessionsMixin:
    """Practice session lifecycle."""

    def create_session(self, session_id: str, game_id: str) -> None:
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "INSERT INTO sessions (id, game_id, started_at) VALUES (?, ?, ?)",
            (session_id, game_id, now),
        )
        self.conn.commit()

    def end_session(self, session_id: str, segments_attempted: int, segments_completed: int) -> None:
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            """UPDATE sessions SET ended_at = ?, segments_attempted = ?, segments_completed = ?
               WHERE id = ?""",
            (now, segments_attempted, segments_completed, session_id),
        )
        self.conn.commit()

    def get_current_session(self, game_id: str) -> Optional[dict]:
        """Get active session (ended_at IS NULL)."""
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE game_id = ? AND ended_at IS NULL "
            "ORDER BY started_at DESC LIMIT 1",
            (game_id,),
        ).fetchone()
        return dict(row) if row else None

    def get_session_history(self, game_id: str, limit: int = 10) -> list[dict]:
        """Recent sessions, most recent first."""
        rows = self.conn.execute(
            """SELECT * FROM sessions
               WHERE game_id = ?
               ORDER BY started_at DESC
               LIMIT ?""",
            (game_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
