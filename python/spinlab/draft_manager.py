# python/spinlab/draft_manager.py
"""DraftManager — owns draft reference lifecycle state."""
from __future__ import annotations

from typing import TYPE_CHECKING

from .models import ActionResult, Status

if TYPE_CHECKING:
    from .db import Database
    from .reference_capture import RefSegmentTime
    from .scheduler import Scheduler


class DraftManager:
    """Manages draft capture runs (pending save/discard after recording or replay)."""

    def __init__(self) -> None:
        self.run_id: str | None = None
        self.segments_count: int = 0

    @property
    def has_draft(self) -> bool:
        return self.run_id is not None

    def enter_draft(self, run_id: str | None, segments_count: int) -> None:
        """Populate draft state from a completed capture/replay."""
        self.run_id = run_id
        self.segments_count = segments_count

    def save(
        self, db: "Database", name: str,
        segment_times: "list[RefSegmentTime] | None" = None,
        scheduler: "Scheduler | None" = None,
    ) -> ActionResult:
        """Promote draft capture run to saved reference, seed attempts, rebuild model."""
        if not self.run_id:
            return ActionResult(status=Status.NO_DRAFT)
        db.promote_draft(self.run_id, name)
        db.set_active_capture_run(self.run_id)

        # Seed reference attempts if timing data is available
        if segment_times:
            from .reference_seeding import seed_reference_attempts
            seed_reference_attempts(db, self.run_id, segment_times)
            if scheduler:
                scheduler.rebuild_all_states()

        self.run_id = None
        self.segments_count = 0
        return ActionResult(status=Status.OK)

    def discard(self, db: "Database") -> ActionResult:
        """Hard-delete draft capture run and all associated data."""
        if not self.run_id:
            return ActionResult(status=Status.NO_DRAFT)
        db.hard_delete_capture_run(self.run_id)
        self.run_id = None
        self.segments_count = 0
        return ActionResult(status=Status.OK)

    def recover(self, db: "Database", game_id: str) -> None:
        """On startup, check for orphaned draft capture runs and restore state."""
        rows = db.conn.execute(
            "SELECT id FROM capture_runs WHERE game_id = ? AND draft = 1 ORDER BY created_at DESC",
            (game_id,),
        ).fetchall()
        if not rows:
            return
        self.run_id = rows[0][0]
        self.segments_count = db.conn.execute(
            "SELECT COUNT(*) FROM segments WHERE reference_id = ? AND active = 1",
            (self.run_id,),
        ).fetchone()[0]
        for row in rows[1:]:
            db.hard_delete_capture_run(row[0])

    def get_state(self) -> dict | None:
        """Return draft dict for get_state() or None."""
        if not self.run_id:
            return None
        return {
            "run_id": self.run_id,
            "segments_captured": self.segments_count,
        }
