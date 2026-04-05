"""CaptureController — orchestrates reference recording and replay capture.

Owns the start/stop lifecycle for both reference and replay modes, routes
capture-related TCP events, and manages the transition into draft state.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from .models import ActionResult, Mode, Status
from .reference_capture import ReferenceCapture
from .draft_manager import DraftManager
from .condition_registry import ConditionRegistry

if TYPE_CHECKING:
    from .db import Database
    from .tcp_manager import TcpManager

logger = logging.getLogger(__name__)


class CaptureController:
    """Manages reference/replay capture and fill-gap flows."""

    def __init__(self, db: "Database", tcp: "TcpManager") -> None:
        self.db = db
        self.tcp = tcp
        self.ref_capture = ReferenceCapture()
        self.draft = DraftManager()
        self.fill_gap_segment_id: str | None = None
        # Empty registry by default; Task 11 will set the real one at startup.
        self.condition_registry: ConditionRegistry = ConditionRegistry()
        # Cold-fill state
        self.cold_fill_queue: list[dict] = []
        self.cold_fill_current: str | None = None
        self.cold_fill_cold_waypoint_id: str | None = None  # waypoint to attach cold state to
        self.cold_fill_total: int = 0

    def set_condition_registry(self, registry: ConditionRegistry) -> None:
        """Replace the condition registry (called at startup with game config)."""
        self.condition_registry = registry

    @property
    def sections_captured(self) -> int:
        return self.ref_capture.segments_count

    @property
    def has_draft(self) -> bool:
        return self.draft.has_draft

    def get_draft_state(self) -> dict | None:
        return self.draft.get_state()

    @property
    def rec_path(self) -> str | None:
        return self.ref_capture.rec_path

    def clear_and_idle(self) -> None:
        """Clear capture state. Caller sets mode to IDLE."""
        self.ref_capture.clear()

    def _game_rec_dir(self, data_dir: Path, game_id: str) -> Path:
        d = data_dir / game_id / "rec"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _enter_draft_from_capture(self) -> None:
        """Transition captured segments into draft state."""
        run_id, count = self.ref_capture.enter_draft()
        self.draft.enter_draft(run_id, count)

    # --- Reference mode ---

    async def start_reference(
        self, mode: Mode,
        game_id: str, data_dir: Path, run_name: str | None = None,
    ) -> ActionResult:
        if self.draft.has_draft:
            return ActionResult(status=Status.DRAFT_PENDING)
        if mode == Mode.PRACTICE:
            return ActionResult(status=Status.PRACTICE_ACTIVE)
        if mode == Mode.REPLAY:
            return ActionResult(status=Status.ALREADY_REPLAYING)
        if not self.tcp.is_connected:
            return ActionResult(status=Status.NOT_CONNECTED)

        self.ref_capture.clear()
        run_id = f"live_{uuid.uuid4().hex[:8]}"
        run_name = run_name or f"Live {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M')}"
        self.db.create_capture_run(run_id, game_id, run_name, draft=True)
        self.ref_capture.capture_run_id = run_id
        rec_path = str(self._game_rec_dir(data_dir, game_id) / f"{run_id}.spinrec")
        await self.tcp.send(json.dumps({"event": "reference_start", "path": rec_path}))
        return ActionResult(status=Status.STARTED, new_mode=Mode.REFERENCE)

    async def stop_reference(self, mode: Mode) -> ActionResult:
        if mode != Mode.REFERENCE:
            return ActionResult(status=Status.NOT_IN_REFERENCE)
        if self.tcp.is_connected:
            await self.tcp.send(json.dumps({"event": "reference_stop"}))
        self._enter_draft_from_capture()
        self.ref_capture.clear()
        return ActionResult(status=Status.STOPPED, new_mode=Mode.IDLE)

    # --- Replay mode ---

    async def start_replay(
        self, mode: Mode,
        game_id: str, spinrec_path: str, speed: int = 0,
    ) -> ActionResult:
        if self.draft.has_draft:
            return ActionResult(status=Status.DRAFT_PENDING)
        if mode == Mode.PRACTICE:
            return ActionResult(status=Status.PRACTICE_ACTIVE)
        if mode == Mode.REFERENCE:
            return ActionResult(status=Status.REFERENCE_ACTIVE)
        if mode == Mode.REPLAY:
            return ActionResult(status=Status.ALREADY_REPLAYING)
        if not self.tcp.is_connected:
            return ActionResult(status=Status.NOT_CONNECTED)

        self.ref_capture.clear()
        run_id = f"replay_{uuid.uuid4().hex[:8]}"
        run_name = f"Replay {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M')}"
        self.db.create_capture_run(run_id, game_id, run_name, draft=True)
        self.ref_capture.capture_run_id = run_id
        await self.tcp.send(json.dumps({"event": "replay", "path": spinrec_path, "speed": speed}))
        return ActionResult(status=Status.STARTED, new_mode=Mode.REPLAY)

    async def stop_replay(self, mode: Mode) -> ActionResult:
        if mode != Mode.REPLAY:
            return ActionResult(status=Status.NOT_REPLAYING)
        if self.tcp.is_connected:
            await self.tcp.send(json.dumps({"event": "replay_stop"}))
        if self.ref_capture.segments_count > 0:
            self._enter_draft_from_capture()
            self.ref_capture.clear()
        else:
            run_id = self.ref_capture.capture_run_id
            self.ref_capture.clear()
            if run_id:
                self.db.hard_delete_capture_run(run_id)
        return ActionResult(status=Status.STOPPED, new_mode=Mode.IDLE)

    # --- Fill-gap ---

    async def start_fill_gap(self, segment_id: str) -> ActionResult:
        if not self.tcp.is_connected:
            return ActionResult(status=Status.NOT_CONNECTED)
        # Look up the start waypoint for this segment and get its hot save state.
        row = self.db.conn.execute(
            "SELECT start_waypoint_id FROM segments WHERE id = ?", (segment_id,)
        ).fetchone()
        start_waypoint_id = row[0] if row else None
        hot = (self.db.get_save_state(start_waypoint_id, "hot")
               if start_waypoint_id else None)
        if not hot:
            return ActionResult(status=Status.NO_HOT_VARIANT)
        self.fill_gap_segment_id = segment_id
        self._fill_gap_waypoint_id = start_waypoint_id
        await self.tcp.send(json.dumps({
            "event": "fill_gap_load",
            "state_path": hot.state_path,
            "message": "Die to capture cold start",
        }))
        return ActionResult(status=Status.STARTED, new_mode=Mode.FILL_GAP)

    def handle_fill_gap_spawn(self, event: dict) -> bool:
        """Returns True if cold save state was captured and mode should return to IDLE."""
        if not event.get("state_captured") or not self.fill_gap_segment_id:
            return False
        waypoint_id = getattr(self, "_fill_gap_waypoint_id", None)
        if waypoint_id:
            from .models import WaypointSaveState
            self.db.add_save_state(WaypointSaveState(
                waypoint_id=waypoint_id,
                variant_type="cold",
                state_path=event["state_path"],
                is_default=True,
            ))
        self.fill_gap_segment_id = None
        self._fill_gap_waypoint_id = None
        return True

    # --- Cold-fill ---

    async def start_cold_fill(self, game_id: str) -> ActionResult:
        if not self.tcp.is_connected:
            return ActionResult(status=Status.NOT_CONNECTED)
        gaps = self.db.segments_missing_cold(game_id)
        if not gaps:
            return ActionResult(status=Status.NO_GAPS)
        self.cold_fill_queue = gaps
        self.cold_fill_total = len(gaps)
        self.cold_fill_current = None
        return await self._load_next_cold_fill()

    async def _load_next_cold_fill(self) -> ActionResult:
        seg = self.cold_fill_queue[0]
        self.cold_fill_current = seg["segment_id"]
        # Look up the start waypoint so handle_cold_fill_spawn knows where to attach cold state.
        row = self.db.conn.execute(
            "SELECT start_waypoint_id FROM segments WHERE id = ?",
            (seg["segment_id"],),
        ).fetchone()
        self.cold_fill_cold_waypoint_id = row[0] if row else None
        await self.tcp.send(json.dumps({
            "event": "cold_fill_load",
            "state_path": seg["hot_state_path"],
            "segment_id": seg["segment_id"],
        }))
        return ActionResult(status=Status.STARTED, new_mode=Mode.COLD_FILL)

    async def handle_cold_fill_spawn(self, event: dict) -> bool:
        """Store cold save state on start waypoint, advance queue. Returns True when all done."""
        if not event.get("state_captured") or not self.cold_fill_current:
            return False
        if self.cold_fill_cold_waypoint_id:
            from .models import WaypointSaveState
            self.db.add_save_state(WaypointSaveState(
                waypoint_id=self.cold_fill_cold_waypoint_id,
                variant_type="cold",
                state_path=event["state_path"],
                is_default=True,
            ))
        self.cold_fill_queue.pop(0)
        if not self.cold_fill_queue:
            self.cold_fill_current = None
            self.cold_fill_cold_waypoint_id = None
            return True
        await self._load_next_cold_fill()
        return False

    def clear_cold_fill(self) -> None:
        """Reset cold-fill state (e.g., on disconnect)."""
        self.cold_fill_queue = []
        self.cold_fill_current = None
        self.cold_fill_total = 0

    def get_cold_fill_state(self) -> dict | None:
        if not self.cold_fill_current:
            return None
        current_num = self.cold_fill_total - len(self.cold_fill_queue) + 1
        seg = self.cold_fill_queue[0] if self.cold_fill_queue else None
        label = ""
        if seg:
            start = "start" if seg["start_type"] == "entrance" else f"cp{seg['start_ordinal']}"
            end = "goal" if seg["end_type"] == "goal" else f"cp{seg['end_ordinal']}"
            label = seg.get("description") or f"L{seg['level_number']} {start} > {end}"
        return {
            "current": current_num,
            "total": self.cold_fill_total,
            "segment_label": label,
        }

    # --- Capture event routing ---

    def handle_entrance(self, event: dict) -> None:
        self.ref_capture.handle_entrance(event)

    def handle_checkpoint(self, event: dict, game_id: str) -> None:
        self.ref_capture.handle_checkpoint(event, game_id, self.db,
                                           self.condition_registry)

    def handle_death(self) -> None:
        self.ref_capture.died = True

    def handle_spawn(self, event: dict, game_id: str) -> None:
        self.ref_capture.handle_spawn(event, game_id, self.db,
                                      self.condition_registry)

    def handle_exit(self, event: dict, game_id: str) -> None:
        self.ref_capture.handle_exit(event, game_id, self.db,
                                     self.condition_registry)

    def handle_rec_saved(self, event: dict) -> None:
        self.ref_capture.rec_path = event.get("path")

    def handle_replay_finished(self) -> None:
        self._enter_draft_from_capture()
        self.ref_capture.clear()

    def handle_replay_error(self) -> None:
        if self.ref_capture.segments_count > 0:
            self._enter_draft_from_capture()
            self.ref_capture.clear()
        else:
            run_id = self.ref_capture.capture_run_id
            self.ref_capture.clear()
            if run_id:
                self.db.hard_delete_capture_run(run_id)

    def handle_disconnect(self) -> None:
        """Handle TCP disconnect — enter draft if segments were captured."""
        if self.ref_capture.segments_count > 0:
            self._enter_draft_from_capture()
            self.ref_capture.clear()
        else:
            run_id = self.ref_capture.capture_run_id
            self.ref_capture.clear()
            if run_id:
                self.db.hard_delete_capture_run(run_id)

    # --- Draft lifecycle ---

    async def save_draft(self, name: str) -> ActionResult:
        return self.draft.save(self.db, name)

    async def discard_draft(self) -> ActionResult:
        return self.draft.discard(self.db)

    def recover_draft(self, game_id: str) -> None:
        self.draft.recover(self.db, game_id)
