"""SegmentRecorder — owns reference/replay segment capture state and logic.

Per-segment timing is written directly to `recorded_segment_times` on segment
close. No in-memory accumulation: a dashboard crash mid-run preserves all
captured timing data in the DB.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..models import EndpointType
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
    type: EndpointType     # ENTRANCE or CHECKPOINT
    ordinal: int
    state_path: str | None
    timestamp_ms: int
    level_num: int
    raw_conditions: dict


class SegmentRecorder:
    """Captures segments during reference runs and replays.

    Stateless across recording sessions: created with a `capture_run_id` and a
    `current_capture_session_id`, and writes directly to the DB. Per-session
    boundaries (death counts, spawn timestamps) reset on `clear()`.
    """

    def __init__(
        self,
        db: "Database",
        condition_registry: "ConditionRegistry",
    ) -> None:
        self._db = db
        self._condition_registry = condition_registry
        self.capture_run_id: str | None = None
        self.current_capture_session_id: str | None = None
        self.pending_start: PendingStart | None = None
        self.died: bool = False
        self.rec_path: str | None = None
        self._deaths_in_segment: int = 0
        self._last_spawn_ms: int | None = None

    def set_condition_registry(self, registry: "ConditionRegistry") -> None:
        """Swap the active condition registry (called on game-switch)."""
        self._condition_registry = registry

    def clear(self) -> None:
        """Reset per-session state. Does NOT clear DB rows."""
        self.capture_run_id = None
        self.current_capture_session_id = None
        self.pending_start = None
        self.died = False
        self.rec_path = None
        self._deaths_in_segment = 0
        self._last_spawn_ms = None

    def handle_entrance(self, event: LevelEntranceEvent) -> None:
        """Buffer a level entrance as pending start."""
        if self.pending_start and self.pending_start.type != "entrance":
            logger.info("Ignoring level_entrance — pending start exists: %s",
                        self.pending_start)
            return
        self.pending_start = PendingStart(
            type=EndpointType.ENTRANCE, ordinal=0,
            state_path=event.state_path, timestamp_ms=event.timestamp_ms,
            level_num=event.level, raw_conditions=event.conditions,
        )
        self.died = False
        self._deaths_in_segment = 0
        self._last_spawn_ms = None

    def _close_segment(self, game_id, start: PendingStart, end_type, end_ordinal,
                       level, end_raw_conditions,
                       end_timestamp_ms: int | None = None) -> None:
        """Create waypoints + segment for the segment ending here, persist timing."""
        from ..models import Segment, Waypoint, WaypointSaveState

        start_conds = self._condition_registry.decode(start.raw_conditions, level=level)
        end_conds = self._condition_registry.decode(end_raw_conditions, level=level)

        wp_start = Waypoint.make(game_id, level, start.type,
                                 start.ordinal, start_conds)
        wp_end = Waypoint.make(game_id, level, end_type, end_ordinal, end_conds)
        self._db.upsert_waypoint(wp_start)
        self._db.upsert_waypoint(wp_end)

        seg_id = Segment.make_id(
            game_id, level, start.type, start.ordinal,
            end_type, end_ordinal, wp_start.id, wp_end.id,
        )
        is_primary = self._compute_is_primary(
            self._db, game_id, level, start.type, start.ordinal,
            end_type, end_ordinal, seg_id)
        existing_count = (
            self._db.count_segments_for_run(self.capture_run_id)
            if self.capture_run_id else 0
        )
        seg = Segment(
            id=seg_id, game_id=game_id, level_number=level,
            start_type=start.type, start_ordinal=start.ordinal,
            end_type=end_type, end_ordinal=end_ordinal,
            start_waypoint_id=wp_start.id, end_waypoint_id=wp_end.id,
            is_primary=is_primary,
            ordinal=existing_count + 1,
            capture_run_id=self.capture_run_id,
            capture_session_id=self.current_capture_session_id,
        )
        self._db.upsert_segment(seg)

        state_path = start.state_path
        if state_path:
            variant = "cold" if start.type == "entrance" else "hot"
            self._db.add_save_state(WaypointSaveState(
                waypoint_id=wp_start.id,
                variant_type=variant,
                state_path=state_path,
            ))

        # Persist timing immediately so a crash before finalize keeps the data.
        start_ts = start.timestamp_ms
        if (start_ts is not None and end_timestamp_ms is not None
                and self.current_capture_session_id is not None):
            time_ms = end_timestamp_ms - start_ts
            deaths = self._deaths_in_segment
            if deaths == 0:
                clean_tail_ms = time_ms
            elif self._last_spawn_ms is not None:
                clean_tail_ms = end_timestamp_ms - self._last_spawn_ms
            else:
                clean_tail_ms = time_ms
            self._db.add_recorded_segment_time(
                self.current_capture_session_id, seg_id,
                time_ms=time_ms, deaths=deaths, clean_tail_ms=clean_tail_ms,
            )

        self._deaths_in_segment = 0
        self._last_spawn_ms = None

    @staticmethod
    def _compute_is_primary(db, game_id, level, start_type, start_ord,
                            end_type, end_ord, new_seg_id) -> bool:
        return not db.has_competing_active_segment(
            game_id=game_id, level=level,
            start_type=start_type, start_ordinal=start_ord,
            end_type=end_type, end_ordinal=end_ord,
            exclude_segment_id=new_seg_id,
        )

    def handle_checkpoint(self, event: CheckpointEvent, game_id: str) -> None:
        if not self.pending_start:
            return
        cp_ordinal = event.cp_ordinal
        level = event.level_num if event.level_num else self.pending_start.level_num
        self._close_segment(
            game_id, self.pending_start, "checkpoint", cp_ordinal,
            level, event.conditions,
            end_timestamp_ms=event.timestamp_ms)
        self.pending_start = PendingStart(
            type=EndpointType.CHECKPOINT, ordinal=cp_ordinal,
            state_path=event.state_path, timestamp_ms=event.timestamp_ms,
            level_num=level, raw_conditions=event.conditions,
        )

    def handle_exit(self, event: LevelExitEvent, game_id: str) -> None:
        if event.goal == "abort":
            self.pending_start = None
            return
        if not self.pending_start:
            return
        level = event.level
        self._close_segment(
            game_id, self.pending_start, "goal", 0,
            level, event.conditions,
            end_timestamp_ms=event.timestamp_ms)
        self.pending_start = None

    def handle_death(self, timestamp_ms: int | None = None) -> None:
        self.died = True
        self._deaths_in_segment += 1

    def handle_spawn_timing(self, timestamp_ms: int | None = None) -> None:
        if timestamp_ms is not None:
            self._last_spawn_ms = timestamp_ms

    def handle_spawn(self, event: SpawnEvent, game_id: str) -> None:
        if not event.is_cold_cp:
            return
        cold_path = event.state_path
        level = event.level_num
        cp_ord = event.cp_ordinal
        if cold_path is None or cp_ord is None:
            return
        from ..models import EndpointType, Waypoint, WaypointSaveState
        conds = self._condition_registry.decode(event.conditions, level=level)
        wp = Waypoint.make(game_id, level, EndpointType.CHECKPOINT, cp_ord, conds)
        self._db.upsert_waypoint(wp)
        self._db.add_save_state(WaypointSaveState(
            waypoint_id=wp.id, variant_type="cold",
            state_path=cold_path))
        logger.debug("Stored cold save state for waypoint %s: %s", wp.id, cold_path)
