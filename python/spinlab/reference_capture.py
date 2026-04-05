"""ReferenceCapture — owns reference/replay segment capture state and logic."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .db import Database
    from .condition_registry import ConditionRegistry

logger = logging.getLogger(__name__)


class ReferenceCapture:
    """Captures segments during reference runs and replays."""

    def __init__(self) -> None:
        self.segments_count: int = 0
        self.capture_run_id: str | None = None
        self.pending_start: dict | None = None
        self.died: bool = False
        self.rec_path: str | None = None

    def clear(self) -> None:
        """Reset all capture state."""
        self.segments_count = 0
        self.capture_run_id = None
        self.pending_start = None
        self.died = False
        self.rec_path = None

    def enter_draft(self) -> tuple[str | None, int]:
        """Return (run_id, segments_count) for draft manager before clearing."""
        return self.capture_run_id, self.segments_count

    def handle_entrance(self, event: dict) -> None:
        """Buffer a level entrance as pending start."""
        # Only set new pending start if we don't already have a checkpoint
        # pending. SMW's level_start can fire spuriously during goal sequences.
        if self.pending_start and self.pending_start["type"] != "entrance":
            logger.info("Ignoring level_entrance — pending start exists: %s",
                        self.pending_start)
            return
        self.pending_start = {
            "type": "entrance",
            "ordinal": 0,
            "state_path": event.get("state_path"),
            "timestamp_ms": 0,
            "level_num": event["level"],
            "raw_conditions": event.get("conditions", {}),
        }
        self.died = False

    def _ensure_capture_run(self, db, game_id) -> None:
        """Create the capture run record if it doesn't already exist."""
        if self.capture_run_id is None:
            return
        existing = db.conn.execute(
            "SELECT id FROM capture_runs WHERE id = ?", (self.capture_run_id,)
        ).fetchone()
        if existing is None:
            db.create_capture_run(self.capture_run_id, game_id,
                                  self.capture_run_id, draft=True)

    def _close_segment(self, db, game_id, start, end_type, end_ordinal,
                       level, end_raw_conditions, registry) -> None:
        """Create waypoints + segment for the segment ending here."""
        from .models import Segment, Waypoint, WaypointSaveState
        self._ensure_capture_run(db, game_id)

        start_conds = registry.decode(start["raw_conditions"], level=level)
        end_conds = registry.decode(end_raw_conditions, level=level)

        wp_start = Waypoint.make(game_id, level, start["type"],
                                 start["ordinal"], start_conds)
        wp_end = Waypoint.make(game_id, level, end_type, end_ordinal, end_conds)
        db.upsert_waypoint(wp_start)
        db.upsert_waypoint(wp_end)

        seg_id = Segment.make_id(
            game_id, level, start["type"], start["ordinal"],
            end_type, end_ordinal, wp_start.id, wp_end.id,
        )
        is_primary = self._compute_is_primary(
            db, game_id, level, start["type"], start["ordinal"],
            end_type, end_ordinal, seg_id)
        self.segments_count += 1
        seg = Segment(
            id=seg_id, game_id=game_id, level_number=level,
            start_type=start["type"], start_ordinal=start["ordinal"],
            end_type=end_type, end_ordinal=end_ordinal,
            start_waypoint_id=wp_start.id, end_waypoint_id=wp_end.id,
            is_primary=is_primary,
            ordinal=self.segments_count,
            reference_id=self.capture_run_id,
        )
        db.upsert_segment(seg)

        state_path = start.get("state_path")
        if state_path:
            variant = "cold" if start["type"] == "entrance" else "hot"
            db.add_save_state(WaypointSaveState(
                waypoint_id=wp_start.id,
                variant_type=variant,
                state_path=state_path,
                is_default=True,
            ))

    @staticmethod
    def _compute_is_primary(db, game_id, level, start_type, start_ord,
                            end_type, end_ord, new_seg_id) -> bool:
        """Return True iff no other active segment exists for this geography."""
        row = db.conn.execute(
            """SELECT id FROM segments
               WHERE game_id = ? AND level_number = ?
               AND start_type = ? AND start_ordinal = ?
               AND end_type = ? AND end_ordinal = ?
               AND active = 1 AND id != ?""",
            (game_id, level, start_type, start_ord,
             end_type, end_ord, new_seg_id),
        ).fetchone()
        return row is None

    def handle_checkpoint(self, event: dict, game_id: str,
                          db: "Database",
                          registry: "ConditionRegistry") -> None:
        """Close current segment at checkpoint, start new one."""
        if not self.pending_start:
            return
        cp_ordinal = event.get("cp_ordinal", 1)
        level = event.get("level_num", self.pending_start["level_num"])
        self._close_segment(
            db, game_id, self.pending_start, "checkpoint", cp_ordinal,
            level, event.get("conditions", {}), registry)
        self.pending_start = {
            "type": "checkpoint",
            "ordinal": cp_ordinal,
            "state_path": event.get("state_path"),
            "timestamp_ms": event.get("timestamp_ms", 0),
            "level_num": level,
            "raw_conditions": event.get("conditions", {}),
        }

    def handle_exit(self, event: dict, game_id: str,
                    db: "Database",
                    registry: "ConditionRegistry") -> None:
        """Pair level_exit with pending start to create final segment."""
        goal = event.get("goal", "abort")
        if goal == "abort":
            self.pending_start = None
            return
        if not self.pending_start:
            return
        level = event["level"]
        self._close_segment(
            db, game_id, self.pending_start, "goal", 0,
            level, event.get("conditions", {}), registry)
        self.pending_start = None

    def handle_spawn(self, event: dict, game_id: str,
                     db: "Database",
                     registry: "ConditionRegistry") -> None:
        """Store cold save state on checkpoint waypoint after a respawn."""
        if not event.get("is_cold_cp") or not event.get("state_captured"):
            return
        cold_path = event.get("state_path")
        level = event.get("level_num")
        cp_ord = event.get("cp_ordinal")
        if cold_path is None or level is None or cp_ord is None:
            return
        from .models import Waypoint, WaypointSaveState
        conds = registry.decode(event.get("conditions", {}), level=level)
        wp = Waypoint.make(game_id, level, "checkpoint", cp_ord, conds)
        db.upsert_waypoint(wp)
        db.add_save_state(WaypointSaveState(
            waypoint_id=wp.id, variant_type="cold",
            state_path=cold_path, is_default=True))
        logger.debug("Stored cold save state for waypoint %s: %s", wp.id, cold_path)
