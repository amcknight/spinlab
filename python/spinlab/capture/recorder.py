"""SegmentRecorder — owns reference/replay segment capture state and logic."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..protocol import (
    CheckpointEvent,
    LevelEntranceEvent,
    LevelExitEvent,
    SpawnEvent,
)

if TYPE_CHECKING:
    from ..condition_registry import ConditionRegistry
    from ..db import Database

logger = logging.getLogger(__name__)


@dataclass
class PendingStart:
    """Buffered start-of-segment state for pairing with the next endpoint."""
    type: str              # "entrance" or "checkpoint"
    ordinal: int
    state_path: str | None
    timestamp_ms: int
    level_num: int
    raw_conditions: dict


@dataclass
class RecordedSegmentTime:
    """Timing data for one segment captured during a reference run."""
    segment_id: str
    time_ms: int
    deaths: int
    clean_tail_ms: int


class SegmentRecorder:
    """Captures segments during reference runs and replays."""

    def __init__(self) -> None:
        self.segments_count: int = 0
        self.capture_run_id: str | None = None
        self.pending_start: PendingStart | None = None
        self.died: bool = False
        self.rec_path: str | None = None
        self.segment_times: list[RecordedSegmentTime] = []
        self._deaths_in_segment: int = 0
        self._last_spawn_ms: int | None = None

    def clear(self) -> None:
        """Reset all capture state."""
        self.segments_count = 0
        self.capture_run_id = None
        self.pending_start = None
        self.died = False
        self.rec_path = None
        self.segment_times = []
        self._deaths_in_segment = 0
        self._last_spawn_ms = None

    def enter_draft(self) -> tuple[str | None, int]:
        """Return (run_id, segments_count) for draft manager before clearing."""
        return self.capture_run_id, self.segments_count

    def handle_entrance(self, event: LevelEntranceEvent) -> None:
        """Buffer a level entrance as pending start."""
        # Only set new pending start if we don't already have a checkpoint
        # pending. SMW's level_start can fire spuriously during goal sequences.
        if self.pending_start and self.pending_start.type != "entrance":
            logger.info("Ignoring level_entrance — pending start exists: %s",
                        self.pending_start)
            return
        self.pending_start = PendingStart(
            type="entrance", ordinal=0,
            state_path=event.state_path, timestamp_ms=event.timestamp_ms,
            level_num=event.level, raw_conditions=event.conditions,
        )
        self.died = False
        self._deaths_in_segment = 0
        self._last_spawn_ms = None

    def _close_segment(self, db, game_id, start: PendingStart, end_type, end_ordinal,
                       level, end_raw_conditions, registry,
                       end_timestamp_ms: int | None = None) -> None:
        """Create waypoints + segment for the segment ending here."""
        from ..models import Segment, Waypoint, WaypointSaveState

        start_conds = registry.decode(start.raw_conditions, level=level)
        end_conds = registry.decode(end_raw_conditions, level=level)

        wp_start = Waypoint.make(game_id, level, start.type,
                                 start.ordinal, start_conds)
        wp_end = Waypoint.make(game_id, level, end_type, end_ordinal, end_conds)
        db.upsert_waypoint(wp_start)
        db.upsert_waypoint(wp_end)

        seg_id = Segment.make_id(
            game_id, level, start.type, start.ordinal,
            end_type, end_ordinal, wp_start.id, wp_end.id,
        )
        is_primary = self._compute_is_primary(
            db, game_id, level, start.type, start.ordinal,
            end_type, end_ordinal, seg_id)
        self.segments_count += 1
        seg = Segment(
            id=seg_id, game_id=game_id, level_number=level,
            start_type=start.type, start_ordinal=start.ordinal,
            end_type=end_type, end_ordinal=end_ordinal,
            start_waypoint_id=wp_start.id, end_waypoint_id=wp_end.id,
            is_primary=is_primary,
            ordinal=self.segments_count,
            reference_id=self.capture_run_id,
        )
        db.upsert_segment(seg)

        state_path = start.state_path
        if state_path:
            variant = "cold" if start.type == "entrance" else "hot"
            db.add_save_state(WaypointSaveState(
                waypoint_id=wp_start.id,
                variant_type=variant,
                state_path=state_path,
                is_default=True,
            ))

        # Record timing if timestamps are available
        start_ts = start.timestamp_ms
        if start_ts is not None and end_timestamp_ms is not None:
            time_ms = end_timestamp_ms - start_ts
            deaths = self._deaths_in_segment
            if deaths == 0:
                clean_tail_ms = time_ms
            elif self._last_spawn_ms is not None:
                clean_tail_ms = end_timestamp_ms - self._last_spawn_ms
            else:
                clean_tail_ms = time_ms  # fallback
            self.segment_times.append(RecordedSegmentTime(
                segment_id=seg_id,
                time_ms=time_ms,
                deaths=deaths,
                clean_tail_ms=clean_tail_ms,
            ))

        # Reset death tracking for next segment
        self._deaths_in_segment = 0
        self._last_spawn_ms = None

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

    def handle_checkpoint(self, event: CheckpointEvent, game_id: str,
                          db: "Database",
                          registry: "ConditionRegistry") -> None:
        """Close current segment at checkpoint, start new one."""
        if not self.pending_start:
            return
        cp_ordinal = event.cp_ordinal
        level = event.level_num if event.level_num else self.pending_start.level_num
        self._close_segment(
            db, game_id, self.pending_start, "checkpoint", cp_ordinal,
            level, event.conditions, registry,
            end_timestamp_ms=event.timestamp_ms)
        self.pending_start = PendingStart(
            type="checkpoint", ordinal=cp_ordinal,
            state_path=event.state_path, timestamp_ms=event.timestamp_ms,
            level_num=level, raw_conditions=event.conditions,
        )

    def handle_exit(self, event: LevelExitEvent, game_id: str,
                    db: "Database",
                    registry: "ConditionRegistry") -> None:
        """Pair level_exit with pending start to create final segment."""
        if event.goal == "abort":
            self.pending_start = None
            return
        if not self.pending_start:
            return
        level = event.level
        self._close_segment(
            db, game_id, self.pending_start, "goal", 0,
            level, event.conditions, registry,
            end_timestamp_ms=event.timestamp_ms)
        self.pending_start = None

    def handle_death(self, timestamp_ms: int | None = None) -> None:
        """Track a death for segment timing. Also sets died flag."""
        self.died = True
        self._deaths_in_segment += 1

    def handle_spawn_timing(self, timestamp_ms: int | None = None) -> None:
        """Track spawn timestamp for clean_tail_ms computation."""
        if timestamp_ms is not None:
            self._last_spawn_ms = timestamp_ms

    def handle_spawn(self, event: SpawnEvent, game_id: str,
                     db: "Database",
                     registry: "ConditionRegistry") -> None:
        """Store cold save state on checkpoint waypoint after a respawn."""
        if not event.is_cold_cp or not event.state_captured:
            return
        cold_path = event.state_path
        level = event.level_num
        cp_ord = event.cp_ordinal
        if cold_path is None or cp_ord is None:
            return
        from ..models import EndpointType, Waypoint, WaypointSaveState
        conds = registry.decode(event.conditions, level=level)
        wp = Waypoint.make(game_id, level, EndpointType.CHECKPOINT, cp_ord, conds)
        db.upsert_waypoint(wp)
        db.add_save_state(WaypointSaveState(
            waypoint_id=wp.id, variant_type="cold",
            state_path=cold_path, is_default=True))
        logger.debug("Stored cold save state for waypoint %s: %s", wp.id, cold_path)
