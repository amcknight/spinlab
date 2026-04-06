# python/spinlab/cold_fill_controller.py
"""ColdFillController — captures cold save states for segments missing them."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .models import ActionResult, Mode, Status, WaypointSaveState
from .protocol import ColdFillLoadCmd

if TYPE_CHECKING:
    from .db import Database
    from .tcp_manager import TcpManager

logger = logging.getLogger(__name__)


class ColdFillController:
    """Manages the cold-fill queue: loads hot states, waits for death+respawn,
    captures the resulting cold save state."""

    def __init__(self, db: "Database", tcp: "TcpManager") -> None:
        self.db = db
        self.tcp = tcp
        self.queue: list[dict] = []
        self.current: str | None = None
        self.cold_waypoint_id: str | None = None
        self.total: int = 0

    async def start(self, game_id: str) -> ActionResult:
        """Begin cold-fill for all segments missing cold save states."""
        if not self.tcp.is_connected:
            return ActionResult(status=Status.NOT_CONNECTED)
        gaps = self.db.segments_missing_cold(game_id)
        if not gaps:
            return ActionResult(status=Status.NO_GAPS)
        self.queue = gaps
        self.total = len(gaps)
        self.current = None
        return await self._load_next()

    async def _load_next(self) -> ActionResult:
        seg = self.queue[0]
        self.current = seg["segment_id"]
        row = self.db.conn.execute(
            "SELECT start_waypoint_id FROM segments WHERE id = ?",
            (seg["segment_id"],),
        ).fetchone()
        self.cold_waypoint_id = row[0] if row else None
        await self.tcp.send_command(ColdFillLoadCmd(
            state_path=seg["hot_state_path"],
            segment_id=seg["segment_id"],
        ))
        return ActionResult(status=Status.STARTED, new_mode=Mode.COLD_FILL)

    async def handle_spawn(self, event: dict) -> bool:
        """Store cold save state, advance queue. Returns True when all done."""
        if not event.get("state_captured") or not self.current:
            return False
        if self.cold_waypoint_id:
            self.db.add_save_state(WaypointSaveState(
                waypoint_id=self.cold_waypoint_id,
                variant_type="cold",
                state_path=event["state_path"],
                is_default=True,
            ))
        self.queue.pop(0)
        if not self.queue:
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
