# python/spinlab/session_manager.py
"""SessionManager — owns all mutable session state for the dashboard."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime, timezone, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from .models import Mode
from .reference_capture import ReferenceCapture
from .draft_manager import DraftManager

if TYPE_CHECKING:
    from .db import Database
    from .tcp_manager import TcpManager

logger = logging.getLogger(__name__)


class SessionManager:
    """Central state owner for the SpinLab dashboard.

    Replaces closure-scoped mutable containers in create_app().
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

        # Session state
        self.mode: Mode = Mode.IDLE
        self.game_id: str | None = None
        self.game_name: str | None = None
        self.scheduler = None  # Scheduler | None, lazy-init
        self.practice_session = None  # PracticeSession | None
        self.practice_task: asyncio.Task | None = None

        # Extracted components
        self.ref_capture = ReferenceCapture()
        self.draft = DraftManager()

        # Fill-gap state
        self.fill_gap_segment_id: str | None = None

        # SSE subscribers
        self._sse_subscribers: list[asyncio.Queue] = []

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
            "sections_captured": self.ref_capture.segments_count,
            "allocator": None,
            "estimator": None,
        }

        if self.game_id is None:
            return base

        sched = self._get_scheduler()
        base["allocator"] = sched.allocator.name
        base["estimator"] = sched.estimator.name

        if self.mode == Mode.PRACTICE and self.practice_session:
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
                    # Attach multi-model outputs for the current segment
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
                    sched = self._get_scheduler()
                    current_seg["selected_model"] = sched.estimator.name
                    base["current_segment"] = current_seg

            queue_ids = sched.peek_next_n(3)
            if ps.current_segment_id:
                queue_ids = [q for q in queue_ids if q != ps.current_segment_id][:2]
            segments_all = self.db.get_all_segments_with_model(self.game_id)
            smap = {s["id"]: s for s in segments_all}
            base["queue"] = [smap[sid] for sid in queue_ids if sid in smap]

        if self.mode == Mode.REPLAY:
            base["replay"] = {"rec_path": self.ref_capture.rec_path}

        draft_state = self.draft.get_state()
        if draft_state:
            base["draft"] = draft_state

        base["recent"] = self.db.get_recent_attempts(self.game_id, limit=8)
        return base

    def _get_scheduler(self):
        """Lazy-init scheduler for current game."""
        if self.scheduler is None:
            from spinlab.scheduler import Scheduler
            self.scheduler = Scheduler(self.db, self._require_game())
        return self.scheduler

    def _require_game(self) -> str:
        """Return current game_id or raise."""
        if self.game_id is None:
            from fastapi import HTTPException
            raise HTTPException(status_code=409, detail="No game loaded")
        return self.game_id

    def _clear_ref_and_idle(self) -> None:
        """Clear reference capture state and set mode to idle."""
        self.ref_capture.clear()
        self.mode = Mode.IDLE

    def _game_rec_dir(self) -> Path:
        """Return the per-game recording directory, creating it if needed."""
        d = self.data_dir / (self.game_id or "unknown") / "rec"
        d.mkdir(parents=True, exist_ok=True)
        return d

    async def switch_game(self, game_id: str, game_name: str) -> None:
        """Switch active game context. Stops any active session first."""
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
        self.draft.recover(self.db, game_id)
        await self._notify_sse()

    # --- SSE ---
    def subscribe_sse(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=16)
        self._sse_subscribers.append(q)
        return q

    def unsubscribe_sse(self, queue: asyncio.Queue) -> None:
        try:
            self._sse_subscribers.remove(queue)
        except ValueError:
            pass

    async def _notify_sse(self) -> None:
        """Push current state to all SSE subscribers."""
        if not self._sse_subscribers:
            return
        state = self.get_state()
        dead: list[asyncio.Queue] = []
        for q in self._sse_subscribers:
            try:
                q.put_nowait(state)
            except asyncio.QueueFull:
                # Drop oldest, push new
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait(state)
                except asyncio.QueueFull:
                    dead.append(q)
        for q in dead:
            self.unsubscribe_sse(q)

    # --- Event routing ---
    async def route_event(self, event: dict) -> None:
        """Single entry point for all TCP events. Routes by type."""
        evt_type = event.get("event")
        handler = self._event_handlers.get(evt_type)
        if handler:
            await handler(event)

    async def _handle_rom_info(self, event: dict) -> None:
        """Auto-discover game from ROM filename."""
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
        self.ref_capture.handle_entrance(event)
        await self._notify_sse()

    async def _handle_checkpoint(self, event: dict) -> None:
        if self.mode not in (Mode.REFERENCE, Mode.REPLAY):
            return
        self.ref_capture.handle_checkpoint(event, self._require_game(), self.db)
        await self._notify_sse()

    async def _handle_death(self, event: dict) -> None:
        if self.mode not in (Mode.REFERENCE, Mode.REPLAY):
            return
        self.ref_capture.died = True

    async def _handle_spawn(self, event: dict) -> None:
        if self.mode == Mode.FILL_GAP:
            await self._handle_fill_gap_spawn(event)
            return
        if self.mode in (Mode.REFERENCE, Mode.REPLAY):
            self.ref_capture.handle_spawn(event, self._require_game(), self.db)

    async def _handle_level_exit(self, event: dict) -> None:
        if self.mode not in (Mode.REFERENCE, Mode.REPLAY):
            return
        logger.info("level_exit: ref_pending_start=%s", self.ref_capture.pending_start)
        self.ref_capture.handle_exit(event, self._require_game(), self.db)
        await self._notify_sse()

    async def _handle_attempt_result(self, event: dict) -> None:
        if self.mode != Mode.PRACTICE:
            return
        if self.practice_session:
            self.practice_session.receive_result(event)
        await self._notify_sse()

    async def _handle_rec_saved(self, event: dict) -> None:
        self.ref_capture.rec_path = event.get("path")

    async def _handle_replay_started(self, event: dict) -> None:
        await self._notify_sse()

    async def _handle_replay_progress(self, event: dict) -> None:
        await self._notify_sse()

    async def _handle_replay_finished(self, event: dict) -> None:
        run_id, count = self.ref_capture.enter_draft()
        self.draft.enter_draft(run_id, count)
        self._clear_ref_and_idle()
        await self._notify_sse()

    async def _handle_replay_error(self, event: dict) -> None:
        if self.ref_capture.segments_count > 0:
            run_id, count = self.ref_capture.enter_draft()
            self.draft.enter_draft(run_id, count)
            self._clear_ref_and_idle()
        else:
            run_id = self.ref_capture.capture_run_id
            self._clear_ref_and_idle()
            if run_id:
                self.db.hard_delete_capture_run(run_id)
        await self._notify_sse()

    # --- Fill-gap mode ---
    async def start_fill_gap(self, segment_id: str) -> dict:
        """Enter fill-gap mode: load hot variant so user can die for cold capture."""
        if not self.tcp.is_connected:
            return {"status": "not_connected"}

        hot = self.db.get_variant(segment_id, "hot")
        if not hot:
            return {"status": "no_hot_variant"}

        self.fill_gap_segment_id = segment_id
        self.mode = Mode.FILL_GAP
        # Load the hot save state
        await self.tcp.send(json.dumps({
            "event": "fill_gap_load",
            "state_path": hot.state_path,
            "message": "Die to capture cold start",
        }))
        await self._notify_sse()
        return {"status": "started", "segment_id": segment_id}

    async def _handle_fill_gap_spawn(self, event: dict) -> None:
        """Handle spawn during fill-gap: capture cold variant."""
        if not event.get("state_captured") or not self.fill_gap_segment_id:
            return
        from .models import SegmentVariant
        variant = SegmentVariant(
            segment_id=self.fill_gap_segment_id,
            variant_type="cold",
            state_path=event["state_path"],
            is_default=True,
        )
        self.db.add_variant(variant)
        self.fill_gap_segment_id = None
        self.mode = Mode.IDLE
        await self._notify_sse()

    # --- Reference mode ---
    async def start_reference(self, run_name: str | None = None) -> dict:
        """Begin reference capture."""
        if self.draft.has_draft:
            return {"status": "draft_pending"}
        if self.mode in (Mode.PRACTICE, Mode.REPLAY):
            return {"status": f"{self.mode.value}_active"}
        if not self.tcp.is_connected:
            return {"status": "not_connected"}
        gid = self._require_game()
        self._clear_ref_and_idle()
        run_id = f"live_{uuid.uuid4().hex[:8]}"
        name = run_name or f"Live {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M')}"
        self.db.create_capture_run(run_id, gid, name, draft=True)
        self.ref_capture.capture_run_id = run_id
        self.mode = Mode.REFERENCE
        rec_path = str(self._game_rec_dir() / f"{run_id}.spinrec")
        await self.tcp.send(json.dumps({"event": "reference_start", "path": rec_path}))
        await self._notify_sse()
        return {"status": "started", "run_id": run_id, "run_name": name}

    async def stop_reference(self) -> dict:
        """End reference capture — enters draft state for save/discard."""
        if self.mode != Mode.REFERENCE:
            return {"status": "not_in_reference"}
        if self.tcp.is_connected:
            await self.tcp.send(json.dumps({"event": "reference_stop"}))
        run_id, count = self.ref_capture.enter_draft()
        self.draft.enter_draft(run_id, count)
        self._clear_ref_and_idle()
        await self._notify_sse()
        return {"status": "stopped"}

    # --- Replay mode ---
    async def start_replay(self, spinrec_path: str, speed: int = 0) -> dict:
        """Begin replay of a .spinrec file."""
        if self.draft.has_draft:
            return {"status": "draft_pending"}
        if self.mode == Mode.PRACTICE:
            return {"status": "practice_active"}
        if self.mode == Mode.REFERENCE:
            return {"status": "reference_active"}
        if self.mode == Mode.REPLAY:
            return {"status": "already_replaying"}
        if not self.tcp.is_connected:
            return {"status": "not_connected"}

        # Set up reference capture so replayed events create segments
        gid = self._require_game()
        self._clear_ref_and_idle()
        run_id = f"replay_{uuid.uuid4().hex[:8]}"
        name = f"Replay {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M')}"
        self.db.create_capture_run(run_id, gid, name, draft=True)
        self.ref_capture.capture_run_id = run_id

        self.mode = Mode.REPLAY
        await self.tcp.send(json.dumps({"event": "replay", "path": spinrec_path, "speed": speed}))
        await self._notify_sse()
        return {"status": "started", "run_id": run_id}

    async def stop_replay(self) -> dict:
        """Abort replay — enters draft state if segments were captured."""
        if self.mode != Mode.REPLAY:
            return {"status": "not_replaying"}
        if self.tcp.is_connected:
            await self.tcp.send(json.dumps({"event": "replay_stop"}))
        if self.ref_capture.segments_count > 0:
            run_id, count = self.ref_capture.enter_draft()
            self.draft.enter_draft(run_id, count)
            self._clear_ref_and_idle()
        else:
            run_id = self.ref_capture.capture_run_id
            self._clear_ref_and_idle()
            if run_id:
                self.db.hard_delete_capture_run(run_id)
        await self._notify_sse()
        return {"status": "stopped"}

    # --- Draft lifecycle ---
    async def save_draft(self, name: str) -> dict:
        """Promote draft capture run to saved reference."""
        result = self.draft.save(self.db, name)
        await self._notify_sse()
        return result

    async def discard_draft(self) -> dict:
        """Hard-delete draft capture run and all associated data."""
        result = self.draft.discard(self.db)
        await self._notify_sse()
        return result

    # --- Practice mode ---
    async def start_practice(self) -> dict:
        """Begin practice session."""
        if self.draft.has_draft:
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
        """Callback when practice task finishes."""
        if self.mode == Mode.PRACTICE:
            self.mode = Mode.IDLE
            asyncio.ensure_future(self._notify_sse())

    async def stop_practice(self) -> dict:
        """Stop practice session."""
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
        """Handle TCP disconnect: stop practice, enter draft if segments captured."""
        if self.practice_session and self.practice_session.is_running:
            self.practice_session.is_running = False
        if self.ref_capture.segments_count > 0:
            run_id, count = self.ref_capture.enter_draft()
            self.draft.enter_draft(run_id, count)
            self._clear_ref_and_idle()
        else:
            run_id = self.ref_capture.capture_run_id
            self._clear_ref_and_idle()
            if run_id:
                self.db.hard_delete_capture_run(run_id)

    async def shutdown(self) -> None:
        """Clean shutdown: stop sessions, close TCP."""
        await self.stop_practice()
        if self.mode == Mode.REFERENCE:
            self._clear_ref_and_idle()
        await self.tcp.disconnect()
