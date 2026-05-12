"""FillGapController — captures the cold start state for a single segment.

Distinct from ColdFillController (which runs through a queue of segments
missing cold states). FillGap is single-shot: user picks one segment, the
hot state loads, the player dies, the next spawn captures the cold state.

State machine: IDLE → ACTIVE (after start()) → IDLE (after consuming a
SpawnEvent with a state_path).
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from spinlab.errors import NoHotVariantError, NotConnectedError
from spinlab.models import ActionResult, Mode, Status, WaypointSaveState
from spinlab.protocol import FillGapLoadCmd, SpawnEvent

if TYPE_CHECKING:
    from spinlab.db import Database
    from spinlab.emu_backend import EmuBackend

logger = logging.getLogger(__name__)


class FillGapController:
    def __init__(self, db: "Database", emu: "EmuBackend") -> None:
        self._db = db
        self._emu = emu
        self._segment_id: str | None = None
        self._waypoint_id: str | None = None

    @property
    def is_active(self) -> bool:
        return self._segment_id is not None

    @property
    def segment_id(self) -> str | None:
        return self._segment_id

    async def start(self, segment_id: str) -> ActionResult:
        if not self._emu.is_connected:
            raise NotConnectedError()
        seg = self._db.get_segment_by_id(segment_id)
        start_waypoint_id = seg.start_waypoint_id if seg else None
        hot = (self._db.get_save_state(start_waypoint_id, "hot")
               if start_waypoint_id else None)
        if not hot:
            raise NoHotVariantError()
        self._segment_id = segment_id
        self._waypoint_id = start_waypoint_id
        await self._emu.send_command(FillGapLoadCmd(
            state_path=hot.state_path,
            message="Die to capture cold start",
        ))
        return ActionResult(status=Status.STARTED, new_mode=Mode.FILL_GAP)

    def handle_spawn(self, event: SpawnEvent) -> bool:
        """Persist cold state if active and event carries a state_path.

        Returns True if the event was consumed (state persisted, controller
        returns to IDLE); False if not active or event has no state_path.
        """
        if not self.is_active or not event.state_path:
            return False
        if self._waypoint_id:
            self._db.add_save_state(WaypointSaveState(
                waypoint_id=self._waypoint_id,
                variant_type="cold",
                state_path=event.state_path,
                is_default=True,
            ))
        self.clear()
        return True

    def clear(self) -> None:
        self._segment_id = None
        self._waypoint_id = None
