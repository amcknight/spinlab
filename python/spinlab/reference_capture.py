# python/spinlab/reference_capture.py
"""ReferenceCapture — owns reference/replay segment capture state and logic."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .db import Database

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
        # pending.  SMW's level_start ($1935) can fire spuriously during
        # goal sequences; overwriting a checkpoint pending_start with an
        # entrance would create wrong segment types.
        if self.pending_start and self.pending_start["type"] != "entrance":
            logger.info("Ignoring level_entrance — pending start exists: %s", self.pending_start)
            return
        self.pending_start = {
            "type": "entrance",
            "ordinal": 0,
            "state_path": event.get("state_path"),
            "timestamp_ms": 0,
            "level_num": event["level"],
        }
        self.died = False

    def handle_checkpoint(self, event: dict, game_id: str, db: "Database") -> None:
        """Handle checkpoint during reference: close current segment, start new one."""
        if not self.pending_start:
            return

        from .models import Segment, SegmentVariant, Waypoint

        start = self.pending_start
        cp_ordinal = event.get("cp_ordinal", 1)
        level = event.get("level_num", start["level_num"])

        # Construct throwaway waypoints with empty conditions to build the segment id.
        # Task 10 will rewrite this to persist real waypoints with actual conditions.
        start_wp = Waypoint.make(game_id, level, start["type"], start["ordinal"], {})
        end_wp = Waypoint.make(game_id, level, "checkpoint", cp_ordinal, {})
        seg_id = Segment.make_id(
            game_id, level,
            start["type"], start["ordinal"],
            "checkpoint", cp_ordinal,
            start_wp.id, end_wp.id,
        )
        self.segments_count += 1
        seg = Segment(
            id=seg_id,
            game_id=game_id,
            level_number=level,
            start_type=start["type"],
            start_ordinal=start["ordinal"],
            end_type="checkpoint",
            end_ordinal=cp_ordinal,
            ordinal=self.segments_count,
            reference_id=self.capture_run_id,
            start_waypoint_id=start_wp.id,
            end_waypoint_id=end_wp.id,
        )
        db.upsert_segment(seg)

        start_path = start.get("state_path")
        if start_path:
            db.add_variant(SegmentVariant(
                segment_id=seg_id,
                variant_type="cold" if start["type"] == "entrance" else "hot",
                state_path=start_path,
                is_default=True,
            ))

        # New pending start is this checkpoint.
        self.pending_start = {
            "type": "checkpoint",
            "ordinal": cp_ordinal,
            "state_path": event.get("state_path"),
            "timestamp_ms": event.get("timestamp_ms", 0),
            "level_num": level,
        }

    def handle_exit(self, event: dict, game_id: str, db: "Database") -> None:
        """Pair level_exit with pending start to create final segment."""
        goal = event.get("goal", "abort")

        if goal == "abort":
            self.pending_start = None
            return

        if not self.pending_start:
            return

        self.segments_count += 1
        from .models import Segment, SegmentVariant, Waypoint
        start = self.pending_start
        level = event["level"]

        end_ordinal = 0
        # Construct throwaway waypoints with empty conditions to build the segment id.
        # Task 10 will rewrite this to persist real waypoints with actual conditions.
        start_wp = Waypoint.make(game_id, level, start["type"], start["ordinal"], {})
        end_wp = Waypoint.make(game_id, level, "goal", end_ordinal, {})
        seg_id = Segment.make_id(
            game_id, level,
            start["type"], start["ordinal"],
            "goal", end_ordinal,
            start_wp.id, end_wp.id,
        )
        seg = Segment(
            id=seg_id,
            game_id=game_id,
            level_number=level,
            start_type=start["type"],
            start_ordinal=start["ordinal"],
            end_type="goal",
            end_ordinal=end_ordinal,
            description="",
            ordinal=self.segments_count,
            reference_id=self.capture_run_id,
            start_waypoint_id=start_wp.id,
            end_waypoint_id=end_wp.id,
        )
        db.upsert_segment(seg)

        state_path = start.get("state_path")
        if state_path:
            db.add_variant(SegmentVariant(
                segment_id=seg_id,
                variant_type="hot" if start["type"] == "checkpoint" else "cold",
                state_path=state_path,
                is_default=True,
            ))

        self.pending_start = None

    def handle_spawn(self, event: dict, game_id: str, db: "Database") -> None:
        """Handle spawn during reference: store cold variant if applicable."""
        if not event.get("is_cold_cp") or not event.get("state_captured"):
            return

        from .models import SegmentVariant

        cold_path = event.get("state_path")
        if not cold_path:
            return

        level = event.get("level_num")
        cp_ord = event.get("cp_ordinal")
        if level is None or cp_ord is None:
            return

        segments = db.get_active_segments(game_id)
        for seg in segments:
            if (seg.level_number == level and seg.start_type == "checkpoint"
                    and seg.start_ordinal == cp_ord):
                variant = SegmentVariant(
                    segment_id=seg.id,
                    variant_type="cold",
                    state_path=cold_path,
                    is_default=True,
                )
                db.add_variant(variant)
                logger.debug("Stored cold variant for segment %s: %s", seg.id, cold_path)
                break
