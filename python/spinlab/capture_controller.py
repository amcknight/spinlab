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

from .models import Mode, SegmentVariant
from .reference_capture import ReferenceCapture
from .draft_manager import DraftManager

if TYPE_CHECKING:
    from .db import Database
    from .tcp_manager import TcpManager

logger = logging.getLogger(__name__)


class CaptureController:
    """Manages reference/replay capture and fill-gap flows."""

    def __init__(self) -> None:
        self.ref_capture = ReferenceCapture()
        self.draft = DraftManager()
        self.fill_gap_segment_id: str | None = None

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
        self, mode: Mode, tcp: "TcpManager", db: "Database",
        game_id: str, data_dir: Path, run_name: str | None = None,
    ) -> dict:
        if self.draft.has_draft:
            return {"status": "draft_pending"}
        if mode in (Mode.PRACTICE, Mode.REPLAY):
            return {"status": f"{mode.value}_active"}
        if not tcp.is_connected:
            return {"status": "not_connected"}

        self.ref_capture.clear()
        run_id = f"live_{uuid.uuid4().hex[:8]}"
        name = run_name or f"Live {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M')}"
        db.create_capture_run(run_id, game_id, name, draft=True)
        self.ref_capture.capture_run_id = run_id
        rec_path = str(self._game_rec_dir(data_dir, game_id) / f"{run_id}.spinrec")
        await tcp.send(json.dumps({"event": "reference_start", "path": rec_path}))
        return {"status": "started", "run_id": run_id, "run_name": name, "new_mode": Mode.REFERENCE}

    async def stop_reference(self, mode: Mode, tcp: "TcpManager") -> dict:
        if mode != Mode.REFERENCE:
            return {"status": "not_in_reference"}
        if tcp.is_connected:
            await tcp.send(json.dumps({"event": "reference_stop"}))
        self._enter_draft_from_capture()
        self.ref_capture.clear()
        return {"status": "stopped", "new_mode": Mode.IDLE}

    # --- Replay mode ---

    async def start_replay(
        self, mode: Mode, tcp: "TcpManager", db: "Database",
        game_id: str, spinrec_path: str, speed: int = 0,
    ) -> dict:
        if self.draft.has_draft:
            return {"status": "draft_pending"}
        if mode == Mode.PRACTICE:
            return {"status": "practice_active"}
        if mode == Mode.REFERENCE:
            return {"status": "reference_active"}
        if mode == Mode.REPLAY:
            return {"status": "already_replaying"}
        if not tcp.is_connected:
            return {"status": "not_connected"}

        self.ref_capture.clear()
        run_id = f"replay_{uuid.uuid4().hex[:8]}"
        name = f"Replay {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M')}"
        db.create_capture_run(run_id, game_id, name, draft=True)
        self.ref_capture.capture_run_id = run_id
        await tcp.send(json.dumps({"event": "replay", "path": spinrec_path, "speed": speed}))
        return {"status": "started", "run_id": run_id, "new_mode": Mode.REPLAY}

    async def stop_replay(self, mode: Mode, tcp: "TcpManager", db: "Database") -> dict:
        if mode != Mode.REPLAY:
            return {"status": "not_replaying"}
        if tcp.is_connected:
            await tcp.send(json.dumps({"event": "replay_stop"}))
        if self.ref_capture.segments_count > 0:
            self._enter_draft_from_capture()
            self.ref_capture.clear()
        else:
            run_id = self.ref_capture.capture_run_id
            self.ref_capture.clear()
            if run_id:
                db.hard_delete_capture_run(run_id)
        return {"status": "stopped", "new_mode": Mode.IDLE}

    # --- Fill-gap ---

    async def start_fill_gap(self, segment_id: str, tcp: "TcpManager", db: "Database") -> dict:
        if not tcp.is_connected:
            return {"status": "not_connected"}
        hot = db.get_variant(segment_id, "hot")
        if not hot:
            return {"status": "no_hot_variant"}
        self.fill_gap_segment_id = segment_id
        await tcp.send(json.dumps({
            "event": "fill_gap_load",
            "state_path": hot.state_path,
            "message": "Die to capture cold start",
        }))
        return {"status": "started", "segment_id": segment_id, "new_mode": Mode.FILL_GAP}

    def handle_fill_gap_spawn(self, event: dict, db: "Database") -> bool:
        """Returns True if cold variant was captured and mode should return to IDLE."""
        if not event.get("state_captured") or not self.fill_gap_segment_id:
            return False
        variant = SegmentVariant(
            segment_id=self.fill_gap_segment_id,
            variant_type="cold",
            state_path=event["state_path"],
            is_default=True,
        )
        db.add_variant(variant)
        self.fill_gap_segment_id = None
        return True

    # --- Capture event routing ---

    def handle_entrance(self, event: dict) -> None:
        self.ref_capture.handle_entrance(event)

    def handle_checkpoint(self, event: dict, game_id: str, db: "Database") -> None:
        self.ref_capture.handle_checkpoint(event, game_id, db)

    def handle_death(self) -> None:
        self.ref_capture.died = True

    def handle_spawn(self, event: dict, game_id: str, db: "Database") -> None:
        self.ref_capture.handle_spawn(event, game_id, db)

    def handle_exit(self, event: dict, game_id: str, db: "Database") -> None:
        self.ref_capture.handle_exit(event, game_id, db)

    def handle_rec_saved(self, event: dict) -> None:
        self.ref_capture.rec_path = event.get("path")

    def handle_replay_finished(self) -> None:
        self._enter_draft_from_capture()
        self.ref_capture.clear()

    def handle_replay_error(self, db: "Database") -> None:
        if self.ref_capture.segments_count > 0:
            self._enter_draft_from_capture()
            self.ref_capture.clear()
        else:
            run_id = self.ref_capture.capture_run_id
            self.ref_capture.clear()
            if run_id:
                db.hard_delete_capture_run(run_id)

    def handle_disconnect(self, db: "Database") -> None:
        """Handle TCP disconnect — enter draft if segments were captured."""
        if self.ref_capture.segments_count > 0:
            self._enter_draft_from_capture()
            self.ref_capture.clear()
        else:
            run_id = self.ref_capture.capture_run_id
            self.ref_capture.clear()
            if run_id:
                db.hard_delete_capture_run(run_id)

    # --- Draft lifecycle ---

    async def save_draft(self, db: "Database", name: str) -> dict:
        return self.draft.save(db, name)

    async def discard_draft(self, db: "Database") -> dict:
        return self.draft.discard(db)

    def recover_draft(self, db: "Database", game_id: str) -> None:
        self.draft.recover(db, game_id)
