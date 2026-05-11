"""ColdFillController — captures cold save states for segments missing them."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..db.segments import MissingColdRow
from ..errors import NotConnectedError
from ..models import ActionResult, Mode, Status, WaypointSaveState
from ..protocol import ColdFillLoadCmd, SpawnEvent

if TYPE_CHECKING:
    from ..db import Database
    from ..emu_backend import EmuBackend

logger = logging.getLogger(__name__)


class ColdFillController:
    """Manages the cold-fill queue: loads hot states, waits for death+respawn,
    captures the resulting cold save state."""

    def __init__(self, db: "Database", emu: "EmuBackend") -> None:
        self.db = db
        self.emu = emu
        self.queue: list[MissingColdRow] = []
        self.current: str | None = None
        self.cold_waypoint_id: str | None = None
        self.total: int = 0

    async def start(self, game_id: str) -> ActionResult:
        """Begin cold-fill for all segments missing cold save states."""
        if not self.emu.is_connected:
            logger.info("cold_fill: skipped — backend not connected")
            raise NotConnectedError()
        gaps = self.db.segments_missing_cold(game_id)
        if not gaps:
            logger.info("cold_fill: no gaps found — all segments have cold states")
            return ActionResult(status=Status.NO_GAPS)
        self.queue = list(gaps)
        self.total = len(gaps)
        self.current = None
        logger.info("cold_fill: starting — %d segments need cold states", self.total)
        return await self._load_next()

    async def _load_next(self) -> ActionResult:
        """Load the next segment's hot state into the emulator.

        Skips segments whose hot state file doesn't exist on disk — that can
        happen when a reference run completed but the underlying state save
        failed (e.g. early dashboard versions where ra_game_basename mismatch
        caused silent SAVE_STATE timeouts). Skipping lets the rest of the
        cold-fill batch proceed; the user gets a warning per skipped segment.
        """
        from pathlib import Path
        while self.queue:
            seg = self.queue[0]
            hot_path = seg["hot_state_path"]
            if not hot_path or not Path(hot_path).is_file():
                logger.warning(
                    "cold_fill: skipping segment %s — hot state missing at %r",
                    seg["segment_id"], hot_path,
                )
                self.queue.pop(0)
                continue
            self.current = seg["segment_id"]
            current_num = self.total - len(self.queue) + 1
            segment_row = self.db.get_segment_by_id(seg["segment_id"])
            self.cold_waypoint_id = segment_row.start_waypoint_id if segment_row else None
            logger.info("cold_fill: loading %d/%d — segment=%s state=%s",
                         current_num, self.total, seg["segment_id"], hot_path)
            await self.emu.send_command(ColdFillLoadCmd(
                state_path=hot_path,
                segment_id=seg["segment_id"],
            ))
            return ActionResult(status=Status.STARTED, new_mode=Mode.COLD_FILL)
        # Drained the queue — every segment had a missing hot state. Surface
        # this as a no-op result so the caller can finalize/return cleanly.
        logger.warning(
            "cold_fill: all %d gap segments had missing hot states; nothing to capture",
            self.total,
        )
        self.current = None
        self.cold_waypoint_id = None
        return ActionResult(status=Status.NO_GAPS)

    async def handle_spawn(self, event: SpawnEvent) -> bool:
        """Store cold save state, advance queue. Returns True when all done."""
        if not self.current:
            logger.warning("cold_fill: spawn received but no current segment")
            return False
        if not event.state_captured or not event.state_path:
            logger.info("cold_fill: spawn without state_captured — ignoring (state_path=%s)",
                        event.state_path)
            return False
        seg_id = event.segment_id or self.current
        try:
            await self.emu.save_state(seg_id)
        except Exception:
            logger.exception("cold_fill: save_state failed for seg_id=%r — "
                             "continuing without storing this cold state", seg_id)
            return False
        logger.info("cold_fill: captured cold state for segment=%s path=%s",
                     self.current, event.state_path)
        if self.cold_waypoint_id:
            self.db.add_save_state(WaypointSaveState(
                waypoint_id=self.cold_waypoint_id,
                variant_type="cold",
                state_path=event.state_path,
                is_default=True,
            ))
        self.queue.pop(0)
        if not self.queue:
            logger.info("cold_fill: complete — all %d cold states captured", self.total)
            self.current = None
            self.cold_waypoint_id = None
            return True
        await self._load_next()
        return False

    def clear(self) -> None:
        """Reset cold-fill state (e.g., on disconnect)."""
        self.queue = []
        self.current = None
        self.total = 0

    def get_state(self) -> dict | None:
        """Return cold-fill progress dict for state snapshots, or None."""
        if not self.current:
            return None
        current_num = self.total - len(self.queue) + 1
        seg = self.queue[0] if self.queue else None
        label = ""
        if seg:
            start = "start" if seg["start_type"] == "entrance" else f"cp{seg['start_ordinal']}"
            end = "goal" if seg["end_type"] == "goal" else f"cp{seg['end_ordinal']}"
            label = seg.get("description") or f"L{seg['level_number']} {start} > {end}"
        return {
            "current": current_num,
            "total": self.total,
            "segment_label": label,
        }
