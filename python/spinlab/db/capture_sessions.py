"""Capture session (recording session within a capture run) queries.

Note on naming: `capture_sessions` are recording sessions within a multi-session
reference run. Distinct from `sessions` (practice / speed-run sessions).
Attempts carry one of two nullable FKs (`session_id` or `capture_run_id`); the
old polymorphic `parent_id` shape was retired in the A0 schema cleanup.
"""
import logging
import sqlite3
from datetime import UTC, datetime
from typing import TypedDict

logger = logging.getLogger(__name__)


class CaptureSessionRow(TypedDict):
    id: str
    capture_run_id: str
    ordinal: int
    started_at: str
    ended_at: str | None
    end_reason: str | None
    segment_count: int


class CaptureSessionsMixin:
    """CRUD and recovery for capture_sessions.

    ``self.transaction()`` is the composable atomic-block CM from ``DatabaseCore``.
    """
    conn: sqlite3.Connection

    # Forward declaration for cross-mixin method call in recover_paused_capture_run.
    # The concrete implementation lives in CaptureRunsMixin; Database inherits both.
    # Safe as a runtime stub because CaptureRunsMixin precedes CaptureSessionsMixin
    # in Database's bases, so MRO finds the real method first.
    def hard_delete_capture_run(self, run_id: str) -> None: ...

    def create_capture_session(
        self, session_id: str, capture_run_id: str,
        ordinal: int,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "INSERT INTO capture_sessions "
            "(id, capture_run_id, ordinal, started_at) "
            "VALUES (?, ?, ?, ?)",
            (session_id, capture_run_id, ordinal, now),
        )

    def get_capture_session(self, session_id: str) -> CaptureSessionRow | None:
        row = self.conn.execute(
            "SELECT s.id, s.capture_run_id, s.ordinal, s.started_at, s.ended_at, "
            "s.end_reason, "
            "(SELECT COUNT(*) FROM segments WHERE capture_session_id = s.id) "
            "  AS segment_count "
            "FROM capture_sessions s WHERE s.id = ?",
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

    def list_capture_sessions_for_run(self, capture_run_id: str) -> list[CaptureSessionRow]:
        rows = self.conn.execute(
            "SELECT s.id, s.capture_run_id, s.ordinal, s.started_at, s.ended_at, "
            "s.end_reason, "
            "(SELECT COUNT(*) FROM segments WHERE capture_session_id = s.id) "
            "  AS segment_count "
            "FROM capture_sessions s "
            "WHERE s.capture_run_id = ? ORDER BY s.ordinal",
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

    def recover_paused_capture_run(self, game_id: str) -> str | None:
        """Find the most recent live-kind draft capture_run for the game.

        Side effects:
        - Hard-deletes any older drafts for the same game (defensive — there
          should only be one paused run per game; if there are more, the
          oldest were stranded and are not recoverable into a coherent state).
          Each discard emits a warning so the loss is visible.
        - Marks any orphaned open sessions for the recovered run as crashed.

        Returns the recovered run id, or None if no draft exists.

        Replay-kind drafts are intentionally excluded: they're ephemeral
        captures that must not be surfaced as recoverable references —
        doing so would clobber a real paused run or appear to the user as
        a resumable reference. Orphaned replay drafts accumulate until
        explicitly discarded.
        """
        with self.transaction():  # type: ignore[attr-defined]
            rows = self.conn.execute(
                "SELECT id FROM capture_runs "
                "WHERE game_id = ? AND status = 'draft' AND kind = 'live' "
                "ORDER BY created_at DESC",
                (game_id,),
            ).fetchall()
            if not rows:
                logger.info("recovery: no paused run for game=%s", game_id)
                return None
            recovered_id = rows[0][0]
            discarded = 0
            for row in rows[1:]:
                logger.warning(
                    "recovery: discarding stranded draft capture_run=%s for game=%s "
                    "(kept newer draft=%s)", row[0], game_id, recovered_id,
                )
                self.hard_delete_capture_run(row[0])
                discarded += 1
            crashed = self.mark_orphan_capture_sessions_crashed(recovered_id)
        logger.info(
            "recovery: kept_run=%s discarded_drafts=%d crashed_sessions=%d",
            recovered_id, discarded, crashed,
        )
        return recovered_id
