# python/spinlab/session_manager.py
"""SessionManager — thin coordinator that delegates to focused controllers."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from .models import Mode
from .capture_controller import CaptureController
from .sse import SSEBroadcaster

if TYPE_CHECKING:
    from .db import Database
    from .tcp_manager import TcpManager

logger = logging.getLogger(__name__)


class SessionManager:
    """Central coordinator for the SpinLab dashboard.

    Owns mode and game context. Delegates capture, SSE, and practice
    to focused components.
    """

    def __init__(
        self,
        db: "Database",
        tcp: "TcpManager",
        rom_dir: Path | None,
        default_category: str = "any%",
        data_dir: Path | None = None,
    ) -> None:
        self.db = db
        self.tcp = tcp
        self.rom_dir = rom_dir
        self.default_category = default_category
        self.data_dir = data_dir or Path("data")

        # Core state
        self.mode: Mode = Mode.IDLE
        self.game_id: str | None = None
        self.game_name: str | None = None
        self.scheduler = None  # Scheduler | None, lazy-init
        self.practice_session = None  # PracticeSession | None
        self.practice_task: asyncio.Task | None = None

        # Delegated components
        self.capture = CaptureController()
        self.sse = SSEBroadcaster()

        # Event dispatch table
        self._event_handlers: dict[str, callable] = {
            "rom_info": self._handle_rom_info,
            "game_context": self._handle_game_context,
            "level_entrance": self._handle_level_entrance,
            "checkpoint": self._handle_checkpoint,
            "death": self._handle_death,
            "spawn": self._handle_spawn,
            "level_exit": self._handle_level_exit,
            "attempt_result": self._handle_attempt_result,
            "rec_saved": self._handle_rec_saved,
            "replay_started": self._handle_replay_started,
            "replay_progress": self._handle_replay_progress,
            "replay_finished": self._handle_replay_finished,
            "replay_error": self._handle_replay_error,
        }

    # --- Backward-compatible properties for tests and dashboard ---

    @property
    def ref_capture(self):
        return self.capture.ref_capture

    @property
    def draft(self):
        return self.capture.draft

    @property
    def fill_gap_segment_id(self):
        return self.capture.fill_gap_segment_id

    @fill_gap_segment_id.setter
    def fill_gap_segment_id(self, value):
        self.capture.fill_gap_segment_id = value

    # --- State ---

    def get_state(self) -> dict:
        """Full state snapshot for API and SSE."""
        base = {
            "mode": self.mode.value,
            "tcp_connected": self.tcp.is_connected,
            "game_id": self.game_id,
            "game_name": self.game_name,
            "current_segment": None,
            "queue": [],
            "recent": [],
            "session": None,
            "sections_captured": self.capture.sections_captured,
            "allocator": None,
            "estimator": None,
        }

        if self.game_id is None:
            return base

        sched = self._get_scheduler()
        base["allocator"] = sched.allocator.name
        base["estimator"] = sched.estimator.name

        if self.mode == Mode.PRACTICE and self.practice_session:
            self._build_practice_state(base, sched)

        if self.mode == Mode.REPLAY:
            base["replay"] = {"rec_path": self.capture.rec_path}

        draft_state = self.capture.get_draft_state()
        if draft_state:
            base["draft"] = draft_state

        base["recent"] = self.db.get_recent_attempts(self.game_id, limit=8)
        return base

    def _build_practice_state(self, base: dict, sched) -> None:
        """Populate practice-specific fields into state dict."""
        ps = self.practice_session
        base["session"] = {
            "id": ps.session_id,
            "started_at": ps.started_at,
            "segments_attempted": ps.segments_attempted,
            "segments_completed": ps.segments_completed,
        }
        if ps.current_segment_id:
            segments = self.db.get_all_segments_with_model(self.game_id)
            seg_map = {s["id"]: s for s in segments}
            if ps.current_segment_id in seg_map:
                current_seg = seg_map[ps.current_segment_id]
                current_seg["attempt_count"] = self.db.get_segment_attempt_count(
                    ps.current_segment_id, ps.session_id
                )
                state_rows = self.db.load_all_model_states_for_segment(ps.current_segment_id)
                model_outputs = {}
                for sr in state_rows:
                    if sr.get("output_json"):
                        try:
                            from spinlab.models import ModelOutput
                            model_outputs[sr["estimator"]] = ModelOutput.from_dict(
                                json.loads(sr["output_json"])
                            ).to_dict()
                        except (json.JSONDecodeError, KeyError):
                            pass
                current_seg["model_outputs"] = model_outputs
                current_seg["selected_model"] = sched.estimator.name
                base["current_segment"] = current_seg

        queue_ids = sched.peek_next_n(3)
        if ps.current_segment_id:
            queue_ids = [q for q in queue_ids if q != ps.current_segment_id][:2]
        segments_all = self.db.get_all_segments_with_model(self.game_id)
        smap = {s["id"]: s for s in segments_all}
        base["queue"] = [smap[sid] for sid in queue_ids if sid in smap]

    def _get_scheduler(self):
        """Lazy-init scheduler for current game."""
        if self.scheduler is None:
            from spinlab.scheduler import Scheduler
            self.scheduler = Scheduler(self.db, self._require_game())
        return self.scheduler

    def _require_game(self) -> str:
        if self.game_id is None:
            from fastapi import HTTPException
            raise HTTPException(status_code=409, detail="No game loaded")
        return self.game_id

    def _clear_ref_and_idle(self) -> None:
        self.capture.clear_and_idle()
        self.mode = Mode.IDLE

    async def switch_game(self, game_id: str, game_name: str) -> None:
        if self.game_id == game_id:
            return
        if self.practice_session and self.practice_session.is_running:
            self.practice_session.is_running = False
        self._clear_ref_and_idle()
        self.db.upsert_game(game_id, game_name, self.default_category)
        self.game_id = game_id
        self.game_name = game_name
        self.scheduler = None
        self.mode = Mode.IDLE
        self.capture.recover_draft(self.db, game_id)
        await self._notify_sse()

    # --- SSE (delegate to broadcaster) ---

    def subscribe_sse(self) -> asyncio.Queue:
        return self.sse.subscribe()

    def unsubscribe_sse(self, queue: asyncio.Queue) -> None:
        self.sse.unsubscribe(queue)

    async def _notify_sse(self) -> None:
        if not self.sse.has_subscribers:
            return
        await self.sse.broadcast(self.get_state())

    # --- Event routing ---

    async def route_event(self, event: dict) -> None:
        evt_type = event.get("event")
        handler = self._event_handlers.get(evt_type)
        if handler:
            await handler(event)

    async def _handle_rom_info(self, event: dict) -> None:
        filename = event.get("filename", "")
        if not self.rom_dir or not filename:
            return
        rom_path = self.rom_dir / filename
        if rom_path.exists():
            from spinlab.romid import rom_checksum, game_name_from_filename
            checksum = rom_checksum(rom_path)
            name = game_name_from_filename(filename)
        else:
            from spinlab.romid import game_name_from_filename
            name = game_name_from_filename(filename)
            checksum = f"file_{name.lower().replace(' ', '_')}"
            logger.warning("ROM not found in rom_dir: %s — using filename as ID", filename)
        await self.switch_game(checksum, name)
        await self.tcp.send(json.dumps({
            "event": "game_context",
            "game_id": checksum,
            "game_name": name,
        }))

    async def _handle_game_context(self, event: dict) -> None:
        gid = event.get("game_id")
        gname = event.get("game_name", gid or "unknown")
        if gid:
            await self.switch_game(gid, gname)

    async def _handle_level_entrance(self, event: dict) -> None:
        if self.mode not in (Mode.REFERENCE, Mode.REPLAY):
            return
        self.capture.handle_entrance(event)
        await self._notify_sse()

    async def _handle_checkpoint(self, event: dict) -> None:
        if self.mode not in (Mode.REFERENCE, Mode.REPLAY):
            return
        self.capture.handle_checkpoint(event, self._require_game(), self.db)
        await self._notify_sse()

    async def _handle_death(self, event: dict) -> None:
        if self.mode not in (Mode.REFERENCE, Mode.REPLAY):
            return
        self.capture.handle_death()

    async def _handle_spawn(self, event: dict) -> None:
        if self.mode == Mode.FILL_GAP:
            if self.capture.handle_fill_gap_spawn(event, self.db):
                self.mode = Mode.IDLE
                await self._notify_sse()
            return
        if self.mode in (Mode.REFERENCE, Mode.REPLAY):
            self.capture.handle_spawn(event, self._require_game(), self.db)

    async def _handle_level_exit(self, event: dict) -> None:
        if self.mode not in (Mode.REFERENCE, Mode.REPLAY):
            return
        logger.info("level_exit: ref_pending_start=%s", self.capture.ref_capture.pending_start)
        self.capture.handle_exit(event, self._require_game(), self.db)
        await self._notify_sse()

    async def _handle_attempt_result(self, event: dict) -> None:
        if self.mode != Mode.PRACTICE:
            return
        if self.practice_session:
            self.practice_session.receive_result(event)
        await self._notify_sse()

    async def _handle_rec_saved(self, event: dict) -> None:
        self.capture.handle_rec_saved(event)

    async def _handle_replay_started(self, event: dict) -> None:
        await self._notify_sse()

    async def _handle_replay_progress(self, event: dict) -> None:
        await self._notify_sse()

    async def _handle_replay_finished(self, event: dict) -> None:
        self.capture.handle_replay_finished()
        self._clear_ref_and_idle()
        await self._notify_sse()

    async def _handle_replay_error(self, event: dict) -> None:
        self.capture.handle_replay_error(self.db)
        self._clear_ref_and_idle()
        await self._notify_sse()

    # --- Mode actions (delegate to controllers, apply mode transitions) ---

    async def start_reference(self, run_name: str | None = None) -> dict:
        result = await self.capture.start_reference(
            self.mode, self.tcp, self.db,
            self._require_game(), self.data_dir, run_name,
        )
        if "new_mode" in result:
            self.mode = result.pop("new_mode")
        await self._notify_sse()
        return result

    async def stop_reference(self) -> dict:
        result = await self.capture.stop_reference(self.mode, self.tcp)
        if "new_mode" in result:
            self.mode = result.pop("new_mode")
        await self._notify_sse()
        return result

    async def start_replay(self, spinrec_path: str, speed: int = 0) -> dict:
        result = await self.capture.start_replay(
            self.mode, self.tcp, self.db,
            self._require_game(), spinrec_path, speed,
        )
        if "new_mode" in result:
            self.mode = result.pop("new_mode")
        await self._notify_sse()
        return result

    async def stop_replay(self) -> dict:
        result = await self.capture.stop_replay(self.mode, self.tcp, self.db)
        if "new_mode" in result:
            self.mode = result.pop("new_mode")
        await self._notify_sse()
        return result

    async def start_fill_gap(self, segment_id: str) -> dict:
        result = await self.capture.start_fill_gap(segment_id, self.tcp, self.db)
        if "new_mode" in result:
            self.mode = result.pop("new_mode")
        await self._notify_sse()
        return result

    async def save_draft(self, name: str) -> dict:
        result = await self.capture.save_draft(self.db, name)
        await self._notify_sse()
        return result

    async def discard_draft(self) -> dict:
        result = await self.capture.discard_draft(self.db)
        await self._notify_sse()
        return result

    # --- Practice mode ---

    async def start_practice(self) -> dict:
        if self.capture.has_draft:
            return {"status": "draft_pending"}
        if self.practice_session and self.practice_session.is_running:
            return {"status": "already_running"}
        if not self.tcp.is_connected:
            return {"status": "not_connected"}
        if self.mode == Mode.REFERENCE:
            self._clear_ref_and_idle()

        from .practice import PracticeSession
        ps = PracticeSession(
            tcp=self.tcp, db=self.db, game_id=self._require_game(),
            on_attempt=lambda _: asyncio.ensure_future(self._notify_sse()),
        )
        self.practice_session = ps
        self.practice_task = asyncio.create_task(ps.run_loop())
        self.practice_task.add_done_callback(self._on_practice_done)
        self.mode = Mode.PRACTICE
        await self._notify_sse()
        return {"status": "started", "session_id": ps.session_id}

    def _on_practice_done(self, task: asyncio.Task) -> None:
        if self.mode == Mode.PRACTICE:
            self.mode = Mode.IDLE
            asyncio.ensure_future(self._notify_sse())

    async def stop_practice(self) -> dict:
        if self.practice_session and self.practice_session.is_running:
            self.practice_session.is_running = False
            if self.practice_task:
                try:
                    await asyncio.wait_for(self.practice_task, timeout=5)
                except asyncio.TimeoutError:
                    self.practice_task.cancel()
            self.mode = Mode.IDLE
            await self._notify_sse()
            return {"status": "stopped"}
        if self.mode == Mode.PRACTICE:
            self.mode = Mode.IDLE
            return {"status": "stopped"}
        return {"status": "not_running"}

    def on_disconnect(self) -> None:
        if self.practice_session and self.practice_session.is_running:
            self.practice_session.is_running = False
        self.capture.handle_disconnect(self.db)
        self._clear_ref_and_idle()

    async def shutdown(self) -> None:
        await self.stop_practice()
        if self.mode == Mode.REFERENCE:
            self._clear_ref_and_idle()
        await self.tcp.disconnect()
