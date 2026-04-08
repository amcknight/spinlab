"""Speed Run session — sequential full-game playthrough with cold recording."""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Callable

from .models import Attempt, AttemptSource
from .protocol import SpeedRunLoadCmd, SpeedRunStopCmd

if TYPE_CHECKING:
    from .db import Database
    from .tcp_manager import TcpManager

logger = logging.getLogger(__name__)

# Maximum seconds to wait for the next event before checking is_running / is_connected.
EVENT_WAIT_TIMEOUT_S = 1.0


@dataclass
class LevelPlan:
    """One level's worth of segments and checkpoint save states."""
    level_number: int
    description: str
    entrance_state_path: str
    segments: list[dict] = field(default_factory=list)
    checkpoints: list[dict] = field(default_factory=list)


class SpeedRunSession:
    """Manages a speed run: plays levels sequentially, records cold attempts."""

    def __init__(
        self,
        tcp: "TcpManager",
        db: "Database",
        game_id: str,
        auto_advance_delay_ms: int = 1000,
        on_event: Callable | None = None,
    ) -> None:
        self.tcp = tcp
        self.db = db
        self.game_id = game_id
        self.auto_advance_delay_ms = auto_advance_delay_ms
        self.on_event = on_event

        self.session_id = uuid.uuid4().hex
        self.started_at = datetime.now(UTC).isoformat()
        self.is_running = False
        self.current_level_index = 0
        self.levels_completed = 0
        self.segments_recorded = 0

        self.levels = self._build_levels()
        self._event_queue: asyncio.Queue = asyncio.Queue()

    def _build_levels(self) -> list[LevelPlan]:
        """Query segments, group into levels, validate save states exist."""
        rows = self.db.get_all_segments_with_model(self.game_id)
        if not rows:
            return []

        levels: list[LevelPlan] = []
        current_level_segs: list[dict] = []

        for row in rows:
            if row["start_type"] == "entrance" and current_level_segs:
                levels.append(self._finalize_level(current_level_segs))
                current_level_segs = []
            current_level_segs.append(row)

        if current_level_segs:
            levels.append(self._finalize_level(current_level_segs))

        return levels

    def _finalize_level(self, segs: list[dict]) -> LevelPlan:
        """Build a LevelPlan from a group of consecutive segments."""
        entrance_seg = segs[0]
        entrance_state = entrance_seg.get("state_path")
        if not entrance_state or not os.path.exists(entrance_state):
            desc = entrance_seg.get("description") or f"L{entrance_seg['level_number']}"
            raise ValueError(
                f"Missing save state for segment {entrance_seg['id']} ({desc})"
            )

        checkpoints = []
        for seg in segs[1:]:
            cp_state = seg.get("state_path")
            if not cp_state or not os.path.exists(cp_state):
                desc = seg.get("description") or f"L{seg['level_number']}"
                raise ValueError(
                    f"Missing save state for segment {seg['id']} ({desc})"
                )
            checkpoints.append({
                "ordinal": seg["start_ordinal"],
                "state_path": cp_state,
            })

        description = entrance_seg.get("description") or f"Level {entrance_seg['level_number']}"

        return LevelPlan(
            level_number=entrance_seg["level_number"],
            description=description,
            entrance_state_path=entrance_state,
            segments=segs,
            checkpoints=checkpoints,
        )

    def start(self) -> None:
        self.db.create_session(self.session_id, self.game_id)
        self.is_running = True
        self.current_level_index = 0
        logger.info(
            "speed_run: started session=%s levels=%d",
            self.session_id[:8], len(self.levels),
        )

    def stop(self) -> None:
        self.is_running = False
        self.db.end_session(
            self.session_id, self.segments_recorded, self.levels_completed,
        )
        logger.info(
            "speed_run: stopped session=%s levels_completed=%d recorded=%d",
            self.session_id[:8], self.levels_completed, self.segments_recorded,
        )

    def receive_event(self, event: dict) -> None:
        """Called by SessionManager when a speed_run_* event arrives."""
        self._event_queue.put_nowait(event)

    async def run_one(self) -> bool:
        """Play one level. Returns False if no more levels."""
        if self.current_level_index >= len(self.levels):
            return False

        level = self.levels[self.current_level_index]

        cmd = SpeedRunLoadCmd(
            id=level.segments[0]["id"],
            state_path=level.entrance_state_path,
            description=level.description,
            checkpoints=level.checkpoints,
            auto_advance_delay_ms=self.auto_advance_delay_ms,
        )

        logger.info(
            "speed_run: loading level %d/%d — %s",
            self.current_level_index + 1, len(self.levels), level.description,
        )
        await self.tcp.send_command(cmd)

        # cold_since tracks whether we are at the start of a segment cold
        # (never seen a warm-up attempt for it this run).  True at level start
        # and after every death; False once a checkpoint is passed cleanly.
        cold_since = True
        current_sub_index = 0

        while self.is_running and self.tcp.is_connected:
            try:
                event = await asyncio.wait_for(
                    self._event_queue.get(), timeout=EVENT_WAIT_TIMEOUT_S
                )
            except asyncio.TimeoutError:
                continue

            event_type = event.get("event")

            if event_type == "speed_run_checkpoint":
                if cold_since and current_sub_index < len(level.segments):
                    self._record_attempt(
                        level.segments[current_sub_index],
                        time_ms=event.get("split_ms", 0),
                        completed=True,
                    )
                current_sub_index += 1
                cold_since = False

            elif event_type == "speed_run_death":
                cold_since = True

            elif event_type == "speed_run_complete":
                if cold_since and current_sub_index < len(level.segments):
                    self._record_attempt(
                        level.segments[current_sub_index],
                        time_ms=event.get("split_ms", 0),
                        completed=True,
                    )
                self.levels_completed += 1
                self.current_level_index += 1
                break

        if self.on_event:
            self.on_event(None)

        return True

    def _record_attempt(self, seg: dict, time_ms: int, completed: bool) -> None:
        """Record a cold attempt for a sub-segment."""
        attempt = Attempt(
            segment_id=seg["id"],
            session_id=self.session_id,
            completed=completed,
            time_ms=time_ms if completed else None,
            deaths=0,
            clean_tail_ms=time_ms if completed else None,
            source=AttemptSource.SPEED_RUN,
        )
        self.db.log_attempt(attempt)
        self.segments_recorded += 1
        logger.info(
            "speed_run: recorded cold attempt segment=%s time=%dms",
            seg["id"], time_ms,
        )

    async def run_loop(self) -> None:
        """Run the full speed run until stopped or all levels done."""
        self.start()
        try:
            while self.is_running and self.tcp.is_connected:
                if not await self.run_one():
                    break
        finally:
            try:
                await self.tcp.send_command(SpeedRunStopCmd())
            except (ConnectionError, OSError):
                pass
            self.stop()
