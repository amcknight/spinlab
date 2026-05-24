"""Hyper Play session — sequential full-game playthrough with cold recording."""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Callable

from spinlab import log

from .db.segments import SegmentRow
from .models import Attempt, AttemptSource
from .protocol import (
    HyperPlayCheckpoint,
    HyperPlayCheckpointEvent,
    HyperPlayCompleteEvent,
    HyperPlayDeathEvent,
    HyperPlayLoadCmd,
    HyperPlayStopCmd,
)

HyperPlayEvent = HyperPlayCheckpointEvent | HyperPlayDeathEvent | HyperPlayCompleteEvent

if TYPE_CHECKING:
    from .db import Database
    from .emu_backend import EmuBackend

logger = logging.getLogger(__name__)

# Maximum seconds to wait for the next event before checking is_running / is_connected.
EVENT_WAIT_TIMEOUT_S = 1.0

# Number of consecutive empty waits (~seconds) after which we log an "still idle"
# progress line. Tuned to ~30 s so the log isn't spammy but a hung level surfaces
# within a single Strafe-window when debugging.
IDLE_PROGRESS_LOG_EVERY = 30


@dataclass
class LevelPlan:
    """One level's worth of segments and checkpoint save states."""
    level_number: int
    description: str
    entrance_state_path: str
    segments: list[SegmentRow] = field(default_factory=list)
    checkpoints: list[HyperPlayCheckpoint] = field(default_factory=list)


class HyperPlaySession:
    """Manages a hyper play run: plays levels sequentially, records cold attempts."""

    def __init__(
        self,
        emu: "EmuBackend",
        db: "Database",
        game_id: str,
        auto_advance_delay_ms: int = 1000,
        death_delay_ms: int = 1500,
        on_event: Callable | None = None,
        session_id: str | None = None,
    ) -> None:
        # death_delay_ms: time the emulator holds a black-screen overlay after
        # a death before reloading the cold save state.  The cold save is
        # captured at the start of SMW's post-respawn fade-in, so reloading it
        # instantly looks like a glitchy replay of the death sequence.  A short
        # blackout gives the death weight without making practice feel sluggish.
        self.emu = emu
        self.db = db
        self.game_id = game_id
        self.auto_advance_delay_ms = auto_advance_delay_ms
        self.death_delay_ms = death_delay_ms
        self.on_event = on_event

        self.session_id = session_id or uuid.uuid4().hex
        self.started_at = datetime.now(UTC).isoformat()
        self.is_running = False
        self.current_level_index = 0
        self.levels_completed = 0
        self.segments_recorded = 0

        self.levels = self._build_levels()
        self._event_queue: asyncio.Queue[HyperPlayEvent] = asyncio.Queue()
        # Attempts FK to sessions(id); the row must exist before any attempt is logged.
        self.db.create_session(self.session_id, self.game_id)

    def _build_levels(self) -> list[LevelPlan]:
        """Query segments, group into levels, validate save states exist."""
        rows = self.db.get_all_segments_with_model(self.game_id)
        if not rows:
            return []

        levels: list[LevelPlan] = []
        current_level_segs: list[SegmentRow] = []

        for row in rows:
            if row["start_type"] == "entrance" and current_level_segs:
                levels.append(self._finalize_level(current_level_segs))
                current_level_segs = []
            current_level_segs.append(row)

        if current_level_segs:
            levels.append(self._finalize_level(current_level_segs))

        return levels

    def _cold_state_for_waypoint(self, waypoint_id: str | None, fallback: str | None) -> str | None:
        """Get cold save state for a waypoint, falling back to default."""
        if waypoint_id:
            cold = self.db.get_save_state(waypoint_id, "cold")
            if cold and os.path.exists(cold.state_path):
                return cold.state_path
        return fallback

    def _cold_state_for_sub_index(self, level: LevelPlan, sub_index: int) -> str | None:
        """Cold-respawn save state for the start of a sub-segment.

        ``sub_index`` matches ``level.segments`` indexing:

        - ``0`` → the level entrance (``level.entrance_state_path``).
        - ``k`` for ``k > 0`` → the ``(k-1)``-th checkpoint's cold state.

        Returns ``None`` if ``sub_index`` is past the last checkpoint (the
        run is between the final checkpoint and ``HyperPlayCompleteEvent``
        — no further reload boundary exists for this level).
        """
        if sub_index == 0:
            return level.entrance_state_path
        cp_index = sub_index - 1
        if 0 <= cp_index < len(level.checkpoints):
            return level.checkpoints[cp_index]["state_path"]
        return None

    def _finalize_level(self, segs: list[SegmentRow]) -> LevelPlan:
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
            # Prefer cold (death-respawn) save state for checkpoint respawn;
            # fall back to default if cold variant hasn't been captured yet.
            default_state = seg.get("state_path")
            cp_state = self._cold_state_for_waypoint(
                seg.get("start_waypoint_id"), default_state,
            )
            if not cp_state or not os.path.exists(cp_state):
                desc = seg.get("description") or f"L{seg['level_number']}"
                raise ValueError(
                    f"Missing save state for segment {seg['id']} ({desc})"
                )
            checkpoints.append(HyperPlayCheckpoint(
                ordinal=seg["start_ordinal"],
                state_path=cp_state,
            ))

        description = entrance_seg.get("description") or f"Level {entrance_seg['level_number']}"

        return LevelPlan(
            level_number=entrance_seg["level_number"],
            description=description,
            entrance_state_path=entrance_state,
            segments=segs,
            checkpoints=checkpoints,
        )

    def start(self) -> None:
        self.is_running = True
        self.current_level_index = 0
        logger.info(
            "hyper_play: started session=%s levels=%d",
            self.session_id[:8], len(self.levels),
        )

    def stop(self) -> None:
        self.is_running = False
        self.db.end_session(
            self.session_id, self.segments_recorded, self.levels_completed,
        )
        logger.info(
            "hyper_play: stopped session=%s levels_completed=%d recorded=%d",
            self.session_id[:8], self.levels_completed, self.segments_recorded,
        )

    def receive_checkpoint(self, event: HyperPlayCheckpointEvent) -> None:
        """Called by SessionManager when a hyper_play_checkpoint event arrives."""
        self._event_queue.put_nowait(event)

    def receive_death(self, event: HyperPlayDeathEvent) -> None:
        """Called by SessionManager when a hyper_play_death event arrives."""
        self._event_queue.put_nowait(event)

    def receive_complete(self, event: HyperPlayCompleteEvent) -> None:
        """Called by SessionManager when a hyper_play_complete event arrives."""
        self._event_queue.put_nowait(event)

    async def run_one(self) -> bool:
        """Play one level. Returns False if no more levels."""
        if self.current_level_index >= len(self.levels):
            return False

        level = self.levels[self.current_level_index]

        cmd = HyperPlayLoadCmd(
            id=level.segments[0]["id"],
            state_path=level.entrance_state_path,
            description=level.description,
            checkpoints=level.checkpoints,
            auto_advance_delay_ms=self.auto_advance_delay_ms,
            death_delay_ms=self.death_delay_ms,
        )

        logger.info(
            "hyper_play: loading level %d/%d — %s",
            self.current_level_index + 1, len(self.levels), level.description,
        )
        await self.emu.send_command(cmd)

        # cold_since tracks whether we are at the start of a segment cold
        # (never seen a warm-up attempt for it this run).  True at level start
        # and after every death; False once a checkpoint is passed cleanly.
        cold_since = True
        current_sub_index = 0
        idle_waits = 0

        while self.is_running and self.emu.is_connected:
            try:
                event = await asyncio.wait_for(
                    self._event_queue.get(), timeout=EVENT_WAIT_TIMEOUT_S
                )
                idle_waits = 0
            except asyncio.TimeoutError:
                idle_waits += 1
                if idle_waits % IDLE_PROGRESS_LOG_EVERY == 0:
                    logger.info(
                        "hyper_play: still idle level=%d/%d sub=%d idle_s=%d",
                        self.current_level_index + 1, len(self.levels),
                        current_sub_index, idle_waits,
                    )
                continue

            if isinstance(event, HyperPlayCheckpointEvent):
                if cold_since and current_sub_index < len(level.segments):
                    self._record_attempt(
                        level.segments[current_sub_index],
                        time_ms=event.split_ms,
                        completed=True,
                    )
                current_sub_index += 1
                cold_since = False

            elif isinstance(event, HyperPlayDeathEvent):
                cold_since = True
                # Reload the cold start of the current sub-segment so the
                # player retries from the same boundary every time. SMW's
                # native respawn drops them at whatever the last in-level
                # checkpoint was, which may or may not match the sub-segment
                # we're currently tracking — explicit reload keeps the two
                # consistent.
                cold_state = self._cold_state_for_sub_index(level, current_sub_index)
                if cold_state is not None:
                    # Match the timing module's blackout duration (timing.py
                    # also waits death_delay_ms before transitioning DYING →
                    # PLAYING) so the visible load lines up with timing's
                    # internal "start of the next attempt" boundary.
                    if self.death_delay_ms > 0:
                        await asyncio.sleep(self.death_delay_ms / 1000)
                    if not self.is_running or not self.emu.is_connected:
                        break
                    try:
                        await self.emu.load_state(cold_state)
                        logger.info(
                            "hyper_play: death → reloaded cold state=%s sub=%d",
                            cold_state, current_sub_index,
                        )
                    except Exception:
                        logger.exception(
                            "hyper_play: load_state on death failed (path=%s)",
                            cold_state,
                        )
                    # Drop anything that landed in the queue during the
                    # blackout window — those events reflect the pre-reload
                    # game state and acting on them now would double-process
                    # whatever just happened.
                    while not self._event_queue.empty():
                        try:
                            self._event_queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break

            elif isinstance(event, HyperPlayCompleteEvent):
                if cold_since and current_sub_index < len(level.segments):
                    self._record_attempt(
                        level.segments[current_sub_index],
                        time_ms=event.split_ms,
                        completed=True,
                    )
                self.levels_completed += 1
                self.current_level_index += 1
                break

        if self.on_event:
            self.on_event(None)

        return True

    def _record_attempt(self, seg: SegmentRow, time_ms: int, completed: bool) -> None:
        """Record a cold attempt for a sub-segment."""
        attempt = Attempt(
            segment_id=seg["id"],
            session_id=self.session_id,
            completed=completed,
            time_ms=time_ms if completed else None,
            deaths=0,
            clean_tail_ms=time_ms if completed else None,
            source=AttemptSource.HYPER_PLAY,
        )
        self.db.log_attempt(attempt)
        self.segments_recorded += 1
        logger.info(
            "hyper_play: recorded cold attempt segment=%s time=%dms",
            seg["id"], time_ms,
        )

    async def run_loop(self) -> None:
        """Run the full hyper play run until stopped or all levels done."""
        self.start()
        try:
            while self.is_running and self.emu.is_connected:
                if not await self.run_one():
                    break
        finally:
            try:
                await self.emu.send_command(HyperPlayStopCmd())
            except (ConnectionError, OSError) as exc:
                log.info(
                    logger, "hyper_play teardown after backend disconnect",
                    exc=exc,
                )
            self.stop()
