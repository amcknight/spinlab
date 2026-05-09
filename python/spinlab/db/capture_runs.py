"""Capture run (reference) queries."""

import sqlite3
from datetime import UTC, datetime
from typing import TypedDict


class CaptureRunRow(TypedDict):
    id: str
    game_id: str
    name: str
    created_at: str
    active: int
    draft: int


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
    ordinal: int | None
    reference_id: str | None
    capture_session_id: str | None
    session_ordinal: int | None
    state_path: str | None


class CaptureRunsMixin:
    """Reference run CRUD and draft lifecycle."""
    conn: sqlite3.Connection

    def create_capture_run(self, run_id: str, game_id: str, name: str, draft: bool = False) -> None:
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "INSERT INTO capture_runs (id, game_id, name, created_at, active, draft) "
            "VALUES (?, ?, ?, ?, 0, ?)",
            (run_id, game_id, name, now, 1 if draft else 0),
        )
        self.conn.commit()

    def list_capture_runs(self, game_id: str) -> list[CaptureRunRow]:
        rows = self.conn.execute(
            "SELECT id, game_id, name, created_at, active, draft FROM capture_runs "
            "WHERE game_id = ? AND draft = 0 ORDER BY created_at",
            (game_id,),
        ).fetchall()
        return [dict(r) for r in rows]  # type: ignore[return-value]

    def set_active_capture_run(self, run_id: str) -> None:
        row = self.conn.execute(
            "SELECT game_id FROM capture_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if not row:
            return
        game_id = row[0]
        self.conn.execute(
            "UPDATE capture_runs SET active = 0 WHERE game_id = ?", (game_id,)
        )
        self.conn.execute(
            "UPDATE capture_runs SET active = 1 WHERE id = ?", (run_id,)
        )
        self.conn.commit()

    def rename_capture_run(self, run_id: str, name: str) -> None:
        self.conn.execute(
            "UPDATE capture_runs SET name = ? WHERE id = ?", (name, run_id)
        )
        self.conn.commit()

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
            "UPDATE segments SET active = 0, reference_id = NULL, updated_at = ? "
            "WHERE reference_id = ?",
            (now, run_id),
        )
        self.conn.execute("DELETE FROM capture_runs WHERE id = ?", (run_id,))
        self.conn.commit()

    def promote_draft(self, run_id: str, name: str) -> None:
        """Promote a draft capture run to saved: rename and set draft=0."""
        self.conn.execute(
            "UPDATE capture_runs SET draft = 0, name = ? WHERE id = ?",
            (name, run_id),
        )
        self.conn.commit()

    def hard_delete_capture_run(self, run_id: str) -> None:
        """Hard delete: remove run, segments, model_state, attempts, sessions, and recorded times."""
        seg_ids = [
            r[0] for r in self.conn.execute(
                "SELECT id FROM segments WHERE reference_id = ?", (run_id,)
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
                "DELETE FROM segments WHERE reference_id = ?", (run_id,),
            )
        # capture_sessions and recorded_segment_times CASCADE from capture_runs
        self.conn.execute("DELETE FROM capture_runs WHERE id = ?", (run_id,))
        self.conn.commit()

    def get_segments_by_reference(self, reference_id: str) -> list[ReferenceSegmentRow]:
        # state_path is always NULL — populate via waypoint_save_states join in caller.
        cur = self.conn.execute(
            """SELECT s.id, s.game_id, s.level_number, s.start_type, s.start_ordinal,
                      s.end_type, s.end_ordinal, s.description, s.active, s.ordinal,
                      s.reference_id, s.capture_session_id,
                      cs.ordinal AS session_ordinal,
                      NULL AS state_path
               FROM segments s
               LEFT JOIN capture_sessions cs ON s.capture_session_id = cs.id
               WHERE s.reference_id = ? AND s.active = 1
               ORDER BY s.ordinal""",
            (reference_id,),
        )
        actual_cols = [desc[0] for desc in cur.description]
        return [dict(zip(actual_cols, row)) for row in cur.fetchall()]  # type: ignore[return-value]
