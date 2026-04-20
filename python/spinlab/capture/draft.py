"""DraftManager — owns draft reference lifecycle state and seeds reference attempts on save."""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ..errors import NoDraftError
from ..models import ActionResult, Attempt, AttemptSource, Status

if TYPE_CHECKING:
    from ..db import Database
    from ..scheduler import Scheduler
    from .recorder import RecordedSegmentTime

logger = logging.getLogger(__name__)


def _seed_reference_attempts(
    db: "Database",
    capture_run_id: str,
    segment_times: list["RecordedSegmentTime"],
) -> int:
    """Insert seed attempts from reference segment times. Returns count inserted."""
    if not segment_times:
        return 0

    now = datetime.now(UTC)
    count = 0
    for rst in segment_times:
        attempt = Attempt(
            segment_id=rst.segment_id,
            session_id=capture_run_id,
            completed=True,
            time_ms=rst.time_ms,
            deaths=rst.deaths,
            clean_tail_ms=rst.clean_tail_ms,
            source=AttemptSource.REFERENCE,
            created_at=now,
        )
        db.log_attempt(attempt)
        count += 1
        logger.info("seed: segment=%s time=%dms deaths=%d clean_tail=%dms",
                     rst.segment_id, rst.time_ms, rst.deaths, rst.clean_tail_ms)

    return count


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
        segment_times: "list[RecordedSegmentTime] | None" = None,
        scheduler: "Scheduler | None" = None,
    ) -> ActionResult:
        """Promote draft capture run to saved reference, seed attempts, rebuild model."""
        if not self.run_id:
            raise NoDraftError()
        db.promote_draft(self.run_id, name)
        db.set_active_capture_run(self.run_id)

        if segment_times:
            _seed_reference_attempts(db, self.run_id, segment_times)
            if scheduler:
                scheduler.rebuild_all_states()

        self.run_id = None
        self.segments_count = 0
        return ActionResult(status=Status.OK)

    def discard(self, db: "Database") -> ActionResult:
        """Hard-delete draft capture run and all associated data."""
        if not self.run_id:
            raise NoDraftError()
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
