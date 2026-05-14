"""Attempt queries."""

import sqlite3
from collections import defaultdict
from typing import TypedDict

from ..models import Attempt


class AttemptRow(TypedDict):
    segment_id: str
    completed: int
    time_ms: int | None
    deaths: int
    clean_tail_ms: int | None
    created_at: str
    invalidated: int


class RecentAttemptRow(TypedDict, total=False):
    id: int
    segment_id: str
    session_id: str | None
    capture_run_id: str | None
    completed: int
    time_ms: int | None
    source: str
    deaths: int
    clean_tail_ms: int | None
    created_at: str
    chosen_allocator: str | None
    description: str
    level_number: int
    start_type: str
    start_ordinal: int
    end_type: str
    end_ordinal: int

RECENT_ATTEMPTS_DB_LIMIT = 5


class AttemptsMixin:
    """Attempt logging and statistics."""
    conn: sqlite3.Connection

    def log_attempt(self, attempt: Attempt) -> int:
        cur = self.conn.execute(
            """INSERT INTO attempts
               (segment_id, session_id, capture_run_id, completed, time_ms,
                source, deaths, clean_tail_ms,
                invalidated, chosen_allocator, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (attempt.segment_id, attempt.session_id, attempt.capture_run_id,
             int(attempt.completed), attempt.time_ms,
             attempt.source, attempt.deaths, attempt.clean_tail_ms,
             int(attempt.invalidated), attempt.chosen_allocator,
             attempt.created_at.isoformat()),
        )
        return cur.lastrowid  # type: ignore[return-value]  # always set after INSERT

    def get_segment_attempt_count(self, segment_id: str, session_id: str) -> int:
        """Count attempts on a segment in a specific practice/speed-run session."""
        row = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM attempts "
            "WHERE segment_id = ? AND session_id = ?",
            (segment_id, session_id),
        ).fetchone()
        return row["cnt"]

    def get_recent_attempts(
        self, game_id: str, limit: int = RECENT_ATTEMPTS_DB_LIMIT,
        session_id: str | None = None,
    ) -> list[RecentAttemptRow]:
        """Last N attempts joined with segment info, most recent first.

        If ``session_id`` is given, filter to attempts from that practice or
        speed-run session.
        """
        where = "s.game_id = ?"
        params: list = [game_id]
        if session_id:
            where += " AND a.session_id = ?"
            params.append(session_id)
        params.append(limit)
        rows = self.conn.execute(
            f"""SELECT a.*, s.description, s.level_number,
                      s.start_type, s.start_ordinal,
                      s.end_type, s.end_ordinal
               FROM attempts a
               JOIN segments s ON a.segment_id = s.id
               WHERE {where}
               ORDER BY a.created_at DESC, a.id DESC
               LIMIT ?""",
            params,
        ).fetchall()
        return [dict(r) for r in rows]  # type: ignore[return-value]

    def get_segment_attempts(self, segment_id: str) -> list[AttemptRow]:
        """Get all attempts for a segment, ordered by created_at."""
        cur = self.conn.execute(
            "SELECT segment_id, completed, time_ms, deaths, clean_tail_ms, created_at, invalidated "
            "FROM attempts WHERE segment_id = ? ORDER BY created_at",
            (segment_id,),
        )
        cols = ["segment_id", "completed", "time_ms", "deaths", "clean_tail_ms", "created_at", "invalidated"]
        return [dict(zip(cols, row)) for row in cur.fetchall()]  # type: ignore[return-value]

    def get_all_attempts_by_segment(self, game_id: str) -> dict[str, list[AttemptRow]]:
        """Load all attempts for all active segments in a game."""
        cur = self.conn.execute(
            """SELECT a.segment_id, a.completed, a.time_ms, a.deaths, a.clean_tail_ms,
                      a.created_at, a.invalidated
               FROM attempts a
               JOIN segments s ON a.segment_id = s.id
               WHERE s.game_id = ? AND s.active = 1
               ORDER BY a.created_at""",
            (game_id,),
        )
        cols = ["segment_id", "completed", "time_ms", "deaths", "clean_tail_ms", "created_at", "invalidated"]
        result: dict[str, list[dict]] = defaultdict(list)
        for row in cur.fetchall():
            d = dict(zip(cols, row))
            result[d["segment_id"]].append(d)
        return result

    def set_attempt_invalidated(self, attempt_id: int, invalidated: bool) -> None:
        self.conn.execute(
            "UPDATE attempts SET invalidated = ? WHERE id = ?",
            (int(invalidated), attempt_id),
        )

    def get_last_practice_attempt(self, session_id: str) -> int | None:
        """Most recent attempt id for a given practice/speed-run session."""
        row = self.conn.execute(
            "SELECT id FROM attempts WHERE session_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        return row[0] if row else None

    def attempt_exists(self, attempt_id: int) -> bool:
        """True if an attempt with this id exists."""
        row = self.conn.execute(
            "SELECT 1 FROM attempts WHERE id = ?", (attempt_id,),
        ).fetchone()
        return row is not None
