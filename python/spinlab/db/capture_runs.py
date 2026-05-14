"""Capture run (reference) queries.

Status / kind / active are orthogonal:
- ``status`` ('draft' | 'saved') — lifecycle. Draft runs are paused; saved runs
  are finalized.
- ``kind`` ('live' | 'replay') — capture method. Live runs come from a real
  recording session; replay runs come from playing back an existing .replay
  file. Recovery on restart only surfaces ``kind='live'`` drafts — replay
  drafts are ephemeral.
- ``active`` (0 | 1) — per-game selection. At most one ``status='saved'`` run
  per game is the chosen reference for practice; only saved runs are
  selectable. Enforced in code (``set_active_capture_run``), not by index.
"""

import sqlite3
from datetime import UTC, datetime
from typing import TypedDict


class CaptureRunRow(TypedDict):
    id: str
    game_id: str
    name: str
    created_at: str
    status: str
    active: int
    kind: str


class ReferenceSegmentRow(TypedDict):
    id: str
    game_id: str
    level_number: int
    start_type: str
    start_ordinal: int
    end_type: str
    end_ordinal: int
    description: str
    active: int
    ordinal: int
    capture_run_id: str | None
    capture_session_id: str | None
    session_ordinal: int | None
    state_path: str | None


class CaptureRunsMixin:
    """Reference run CRUD and draft lifecycle.

    Methods that need internal atomicity call ``self.transaction()``, provided
    by ``DatabaseCore`` via composition in the ``Database`` class. No forward
    declaration here — adding a runtime stub would shadow the real method via
    MRO, and a TYPE_CHECKING stub would clash on the return type.
    """
    conn: sqlite3.Connection

    def create_capture_run(
        self, run_id: str, game_id: str, name: str,
        *, kind: str = "live",
    ) -> None:
        """Create a fresh draft capture_run. ``kind`` is 'live' or 'replay'."""
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "INSERT INTO capture_runs (id, game_id, name, created_at, status, active, kind) "
            "VALUES (?, ?, ?, ?, 'draft', 0, ?)",
            (run_id, game_id, name, now, kind),
        )

    def list_capture_runs(self, game_id: str) -> list[CaptureRunRow]:
        """List finalized capture_runs for a game (drafts excluded)."""
        rows = self.conn.execute(
            "SELECT id, game_id, name, created_at, status, active, kind "
            "FROM capture_runs "
            "WHERE game_id = ? AND status = 'saved' ORDER BY created_at",
            (game_id,),
        ).fetchall()
        return [dict(r) for r in rows]  # type: ignore[return-value]

    def set_active_capture_run(self, run_id: str) -> None:
        """Mark ``run_id`` as the active reference for its game.

        No-op if ``run_id`` doesn't exist. Only saved runs can be active; this
        is enforced by callers (finalize sets status='saved' first).
        """
        row = self.conn.execute(
            "SELECT game_id FROM capture_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if not row:
            return
        game_id = row[0]
        with self.transaction():  # type: ignore[attr-defined]
            self.conn.execute(
                "UPDATE capture_runs SET active = 0 WHERE game_id = ?", (game_id,)
            )
            self.conn.execute(
                "UPDATE capture_runs SET active = 1 WHERE id = ?", (run_id,)
            )

    def rename_capture_run(self, run_id: str, name: str) -> None:
        self.conn.execute(
            "UPDATE capture_runs SET name = ? WHERE id = ?", (name, run_id)
        )

    def delete_capture_run(self, run_id: str) -> None:
        """Soft-delete: deactivate all segments in the run, null FKs, remove the record.

        Also nulls segments.capture_session_id so that the cascade chain
        (capture_runs → capture_sessions → segments via the ON DELETE CASCADE
        on segments.capture_session_id) doesn't cascade-delete segments
        out from under their non-cascading attempts FK. Without this, a
        soft-delete on a run with seeded attempts raises FOREIGN KEY constraint
        failed.
        """
        now = datetime.now(UTC).isoformat()
        with self.transaction():  # type: ignore[attr-defined]
            # Break the segments→capture_sessions cascade so segments stay alive.
            self.conn.execute(
                """
                UPDATE segments SET capture_session_id = NULL, updated_at = ?
                WHERE capture_session_id IN (
                    SELECT id FROM capture_sessions WHERE capture_run_id = ?
                )
                """,
                (now, run_id),
            )
            self.conn.execute(
                "UPDATE segments SET active = 0, capture_run_id = NULL, updated_at = ? "
                "WHERE capture_run_id = ?",
                (now, run_id),
            )
            self.conn.execute("DELETE FROM capture_runs WHERE id = ?", (run_id,))

    def promote_draft(self, run_id: str, name: str) -> None:
        """Promote a draft capture run to saved: rename and set status='saved'."""
        self.conn.execute(
            "UPDATE capture_runs SET status = 'saved', name = ? WHERE id = ?",
            (name, run_id),
        )

    def is_run_draft(self, run_id: str) -> bool:
        """True if ``run_id`` exists and is in draft state. Missing runs return False."""
        row = self.conn.execute(
            "SELECT status FROM capture_runs WHERE id = ?", (run_id,),
        ).fetchone()
        return bool(row and row[0] == "draft")

    def hard_delete_capture_run(self, run_id: str) -> None:
        """Hard delete: remove run, segments, model_state, attempts, and recorded times."""
        with self.transaction():  # type: ignore[attr-defined]
            seg_ids = [
                r[0] for r in self.conn.execute(
                    "SELECT id FROM segments WHERE capture_run_id = ?", (run_id,)
                ).fetchall()
            ]
            if seg_ids:
                placeholders = ",".join("?" * len(seg_ids))
                self.conn.execute(
                    f"DELETE FROM model_state WHERE segment_id IN ({placeholders})",
                    seg_ids,
                )
                self.conn.execute(
                    f"DELETE FROM attempts WHERE segment_id IN ({placeholders})",
                    seg_ids,
                )
                self.conn.execute(
                    "DELETE FROM segments WHERE capture_run_id = ?", (run_id,),
                )
            # capture_sessions and recorded_segment_times CASCADE from capture_runs
            self.conn.execute("DELETE FROM capture_runs WHERE id = ?", (run_id,))

    def get_segments_by_reference(self, capture_run_id: str) -> list[ReferenceSegmentRow]:
        # state_path is always NULL — populate via waypoint_save_states join in caller.
        cur = self.conn.execute(
            """SELECT s.id, s.game_id, s.level_number, s.start_type, s.start_ordinal,
                      s.end_type, s.end_ordinal, s.description, s.active, s.ordinal,
                      s.capture_run_id, s.capture_session_id,
                      cs.ordinal AS session_ordinal,
                      NULL AS state_path
               FROM segments s
               LEFT JOIN capture_sessions cs ON s.capture_session_id = cs.id
               WHERE s.capture_run_id = ? AND s.active = 1
               ORDER BY s.ordinal""",
            (capture_run_id,),
        )
        actual_cols = [desc[0] for desc in cur.description]
        return [dict(zip(actual_cols, row)) for row in cur.fetchall()]  # type: ignore[return-value]
