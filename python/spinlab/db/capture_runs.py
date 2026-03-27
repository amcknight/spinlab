"""Capture run (reference) queries."""

from datetime import UTC, datetime


class CaptureRunsMixin:
    """Reference run CRUD and draft lifecycle."""

    def create_capture_run(self, run_id: str, game_id: str, name: str, draft: bool = False) -> None:
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "INSERT INTO capture_runs (id, game_id, name, created_at, active, draft) "
            "VALUES (?, ?, ?, ?, 0, ?)",
            (run_id, game_id, name, now, 1 if draft else 0),
        )
        self.conn.commit()

    def list_capture_runs(self, game_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, game_id, name, created_at, active, draft FROM capture_runs "
            "WHERE game_id = ? AND draft = 0 ORDER BY created_at",
            (game_id,),
        ).fetchall()
        return [dict(r) for r in rows]

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
        """Soft-delete: deactivate all segments in the run, null FK, remove the record."""
        now = datetime.now(UTC).isoformat()
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
        """Hard delete: remove run, segments, variants, model_state, attempts."""
        seg_ids = [
            r[0] for r in self.conn.execute(
                "SELECT id FROM segments WHERE reference_id = ?", (run_id,)
            ).fetchall()
        ]
        if seg_ids:
            placeholders = ",".join("?" * len(seg_ids))
            self.conn.execute(
                f"DELETE FROM segment_variants WHERE segment_id IN ({placeholders})",
                seg_ids,
            )
            self.conn.execute(
                f"DELETE FROM model_state WHERE segment_id IN ({placeholders})",
                seg_ids,
            )
            self.conn.execute(
                f"DELETE FROM attempts WHERE segment_id IN ({placeholders})",
                seg_ids,
            )
            self.conn.execute(
                f"DELETE FROM segments WHERE reference_id = ?", (run_id,),
            )
        self.conn.execute("DELETE FROM capture_runs WHERE id = ?", (run_id,))
        self.conn.commit()

    def get_segments_by_reference(self, reference_id: str) -> list[dict]:
        cur = self.conn.execute(
            """SELECT id, game_id, level_number, start_type, start_ordinal,
                      end_type, end_ordinal, description, active, ordinal,
                      reference_id,
                      (SELECT sv.state_path FROM segment_variants sv
                       WHERE sv.segment_id = segments.id
                       ORDER BY sv.is_default DESC LIMIT 1) AS state_path
               FROM segments WHERE reference_id = ? AND active = 1
               ORDER BY ordinal""",
            (reference_id,),
        )
        actual_cols = [desc[0] for desc in cur.description]
        return [dict(zip(actual_cols, row)) for row in cur.fetchall()]
