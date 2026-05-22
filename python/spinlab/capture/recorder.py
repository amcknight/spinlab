"""SegmentRecorder — owns reference/replay segment capture state and logic.

Per-event attempt rows are buffered in memory during a segment, then flushed
to `attempts` atomically with the segment upsert at segment close. A dashboard
crash mid-segment loses the in-flight segment's buffered events; completed
segments are durable. Same crash-safety bound as the pre-2026-05 design
that drained `recorded_segment_times` at finalize.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ..models import (
    AttemptOutcome,
    AttemptSource,
    EndpointType,
    EventAttempt,
)
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
    raw_conditions: dict[str, int]


@dataclass
class _PendingEvent:
    """One died/survived event buffered until the segment closes (when the
    real `segment_id` becomes known and the rows can be written together).
    """
    outcome: AttemptOutcome
    time_ms: int
    created_at: datetime


class SegmentRecorder:
    """Captures segments during reference runs and replays.

    Stateless across recording sessions: created with a `capture_run_id` and a
    `current_capture_session_id`, and writes directly to the DB. Per-segment
    boundaries (death counts, episode_id, pending events) reset on `clear()`.
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

        # Per-segment episode tracking (one fresh episode_id per segment-pass).
        # Minted at handle_entrance / handle_checkpoint(segment-start) and
        # carried into every EventAttempt buffered for this segment.
        self._episode_id: str = ""
        # Wall-clock of the previous event in this episode (or segment-start
        # before the first event). `time_ms` per event = now - _last_event_ms.
        self._last_event_ms: int = 0
        # Buffered events for the in-flight segment; flushed atomically with
        # the segment upsert at _close_segment.
        self._pending_events: list[_PendingEvent] = []

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
        self._episode_id = ""
        self._last_event_ms = 0
        self._pending_events = []

    def _arm_new_episode(self, start_ts_ms: int) -> None:
        """Mint a fresh episode_id for the upcoming segment and reset
        per-segment buffer/counters."""
        self._episode_id = uuid.uuid4().hex
        self._last_event_ms = start_ts_ms
        self._pending_events = []
        self._deaths_in_segment = 0
        self._last_spawn_ms = None
        self.died = False

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
        self._arm_new_episode(event.timestamp_ms)

    def _close_segment(self, game_id, start: PendingStart, end_type, end_ordinal,
                       level, end_raw_conditions,
                       end_timestamp_ms: int | None = None) -> None:
        """Create waypoints + segment for the segment ending here, then flush
        all buffered events (deaths + closing survived) into `attempts` —
        atomically with the segment upsert.
        """
        from ..models import Segment, Waypoint, WaypointSaveState

        start_conds = self._condition_registry.decode(start.raw_conditions, level=level)
        end_conds = self._condition_registry.decode(end_raw_conditions, level=level)

        wp_start = Waypoint.make(game_id, level, start.type,
                                 start.ordinal, start_conds)
        wp_end = Waypoint.make(game_id, level, end_type, end_ordinal, end_conds)
        seg_id = Segment.make_id(
            game_id, level, start.type, start.ordinal,
            end_type, end_ordinal, wp_start.id, wp_end.id,
        )

        # Build the closing "survived" event before opening the transaction
        # so the buffer is fully assembled before we touch the DB.
        survived_event: _PendingEvent | None = None
        if (start.timestamp_ms is not None and end_timestamp_ms is not None
                and self.current_capture_session_id is not None
                and self.capture_run_id is not None):
            survived_event = _PendingEvent(
                outcome=AttemptOutcome.SURVIVED,
                time_ms=end_timestamp_ms - self._last_event_ms,
                created_at=datetime.now(UTC),
            )

        with self._db.transaction():
            self._db.upsert_waypoint(wp_start)
            self._db.upsert_waypoint(wp_end)
            is_primary = not self._db.has_competing_active_segment(
                game_id=game_id, level=level,
                start_type=start.type, start_ordinal=start.ordinal,
                end_type=end_type, end_ordinal=end_ordinal,
                exclude_segment_id=seg_id,
            )
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

            # Flush buffered events + closing survived event. All keyed to
            # the just-upserted segment_id so the FK is satisfied.
            events_to_write = list(self._pending_events)
            if survived_event is not None:
                events_to_write.append(survived_event)
            if events_to_write and self.capture_run_id is not None:
                for ev in events_to_write:
                    self._db.log_event_attempt(EventAttempt(
                        segment_id=seg_id,
                        episode_id=self._episode_id,
                        outcome=ev.outcome,
                        time_ms=ev.time_ms,
                        capture_run_id=self.capture_run_id,
                        source=AttemptSource.REFERENCE,
                        created_at=ev.created_at,
                    ))
                logger.info(
                    "recorder: flushed %d events for segment=%s (deaths=%d)",
                    len(events_to_write), seg_id, self._deaths_in_segment,
                )

        self._pending_events = []
        self._deaths_in_segment = 0
        self._last_spawn_ms = None

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
        # New segment starts here — fresh episode for the cp→next pass.
        self._arm_new_episode(event.timestamp_ms)

    def handle_exit(self, event: LevelExitEvent, game_id: str) -> None:
        if event.goal == "abort":
            # Drop the in-flight buffer; the segment never closes so no
            # events get written.
            self.pending_start = None
            self._pending_events = []
            self._deaths_in_segment = 0
            self._last_spawn_ms = None
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
        # Buffer the died event with raw wall-clock delta since the previous
        # event (or segment-start, for the first death). The penalty math
        # lives in the legacy roll-up adapter, not at write time.
        # When timestamp_ms is None (e.g. called from ReferenceController
        # without a clock source), use time_ms=0 — the event still occurred,
        # we just lack a precise delta.
        delta = (timestamp_ms - self._last_event_ms) if timestamp_ms is not None else 0
        self._pending_events.append(_PendingEvent(
            outcome=AttemptOutcome.DIED,
            time_ms=delta,
            created_at=datetime.now(UTC),
        ))
        if timestamp_ms is not None:
            self._last_event_ms = timestamp_ms

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
