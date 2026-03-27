"""Attempt queries."""

from collections import defaultdict
from datetime import UTC, datetime
from typing import Optional

from ..models import Attempt


class AttemptsMixin:
    """Attempt logging and statistics."""

    def log_attempt(self, attempt: Attempt) -> None:
        self.conn.execute(
            """INSERT INTO attempts
               (segment_id, session_id, completed, time_ms, goal_matched,
                rating, strat_version, source, deaths, clean_tail_ms, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (attempt.segment_id, attempt.session_id, int(attempt.completed),
             attempt.time_ms, attempt.goal_matched,
             attempt.rating,
             attempt.strat_version, attempt.source,
             attempt.deaths, attempt.clean_tail_ms,
             attempt.created_at.isoformat()),
        )
        self.conn.commit()

    def get_segment_stats(self, segment_id: str, strat_version: Optional[int] = None) -> dict:
        """Get aggregate stats for a segment."""
        where = "segment_id = ?"
        params: list = [segment_id]
        if strat_version is not None:
            where += " AND strat_version = ?"
            params.append(strat_version)

        row = self.conn.execute(
            f"""SELECT
                COUNT(*) as total_attempts,
                SUM(completed) as completions,
                AVG(CASE WHEN completed = 1 THEN time_ms END) as avg_time_ms,
                MIN(CASE WHEN completed = 1 THEN time_ms END) as best_time_ms
            FROM attempts WHERE {where}""",
            params,
        ).fetchone()
        return dict(row)

    def get_segment_attempt_count(self, segment_id: str, session_id: str) -> int:
        """Count attempts on a segment in a specific session."""
        row = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM attempts "
            "WHERE segment_id = ? AND session_id = ?",
            (segment_id, session_id),
        ).fetchone()
        return row["cnt"]

    def get_recent_attempts(self, game_id: str, limit: int = 8) -> list[dict]:
        """Last N attempts joined with segment info, most recent first."""
        rows = self.conn.execute(
            """SELECT a.*, s.description, s.level_number,
                      s.start_type, s.start_ordinal,
                      s.end_type, s.end_ordinal
               FROM attempts a
               JOIN segments s ON a.segment_id = s.id
               WHERE s.game_id = ?
               ORDER BY a.created_at DESC, a.id DESC
               LIMIT ?""",
            (game_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_segment_attempts(self, segment_id: str) -> list[dict]:
        """Get all attempts for a segment, ordered by created_at."""
        cur = self.conn.execute(
            "SELECT segment_id, completed, time_ms, deaths, clean_tail_ms, created_at "
            "FROM attempts WHERE segment_id = ? ORDER BY created_at",
            (segment_id,),
        )
        cols = ["segment_id", "completed", "time_ms", "deaths", "clean_tail_ms", "created_at"]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def get_all_attempts_by_segment(self, game_id: str) -> dict[str, list[dict]]:
        """Load all attempts for all active segments in a game."""
        cur = self.conn.execute(
            """SELECT a.segment_id, a.completed, a.time_ms, a.deaths, a.clean_tail_ms,
                      a.created_at
               FROM attempts a
               JOIN segments s ON a.segment_id = s.id
               WHERE s.game_id = ? AND s.active = 1
               ORDER BY a.created_at""",
            (game_id,),
        )
        cols = ["segment_id", "completed", "time_ms", "deaths", "clean_tail_ms", "created_at"]
        result: dict[str, list[dict]] = defaultdict(list)
        for row in cur.fetchall():
            d = dict(zip(cols, row))
            result[d["segment_id"]].append(d)
        return result
