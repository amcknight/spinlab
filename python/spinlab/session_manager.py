# python/spinlab/session_manager.py
"""SessionManager — owns all mutable session state for the dashboard."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

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
    ) -> None:
        self.db = db
        self.tcp = tcp
        self.rom_dir = rom_dir
        self.default_category = default_category

        # Session state
        self.mode: str = "idle"  # "idle" | "reference" | "practice"
        self.game_id: str | None = None
        self.game_name: str | None = None
        self.scheduler = None  # Scheduler | None, lazy-init
        self.practice_session = None  # PracticeSession | None
        self.practice_task: asyncio.Task | None = None

        # Reference capture state
        self.ref_pending: dict[tuple, dict] = {}
        self.ref_segments_count: int = 0
        self.ref_capture_run_id: str | None = None
        self.ref_pending_start: dict | None = None
        self.ref_died: bool = False

        # SSE subscribers
        self._sse_subscribers: list[asyncio.Queue] = []

    def get_state(self) -> dict:
        """Full state snapshot for API and SSE."""
        base = {
            "mode": self.mode,
            "tcp_connected": self.tcp.is_connected,
            "game_id": self.game_id,
            "game_name": self.game_name,
            "current_segment": None,
            "queue": [],
            "recent": [],
            "session": None,
            "sections_captured": self.ref_segments_count,
            "allocator": None,
            "estimator": None,
        }

        if self.game_id is None:
            return base

        sched = self._get_scheduler()
        base["allocator"] = sched.allocator.name
        base["estimator"] = sched.estimator.name

        if self.mode == "practice" and self.practice_session:
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
                    model_row = self.db.load_model_state(ps.current_segment_id)
                    if model_row and model_row["state_json"]:
                        from spinlab.estimators.kalman import KalmanState
                        from spinlab.estimators import get_estimator
                        state = KalmanState.from_dict(json.loads(model_row["state_json"]))
                        est = get_estimator(model_row["estimator"])
                        current_seg["drift_info"] = est.drift_info(state)
                    base["current_segment"] = current_seg

            queue_ids = sched.peek_next_n(3)
            if ps.current_segment_id:
                queue_ids = [q for q in queue_ids if q != ps.current_segment_id][:2]
            segments_all = self.db.get_all_segments_with_model(self.game_id)
            smap = {s["id"]: s for s in segments_all}
            base["queue"] = [smap[sid] for sid in queue_ids if sid in smap]

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

    def _clear_ref_state(self) -> None:
        """Clear reference capture state."""
        self.ref_pending.clear()
        self.ref_segments_count = 0
        self.ref_capture_run_id = None
        self.ref_pending_start = None
        self.ref_died = False
        self.mode = "idle"

    async def switch_game(self, game_id: str, game_name: str) -> None:
        """Switch active game context. Stops any active session first."""
        if self.game_id == game_id:
            return

        if self.practice_session and self.practice_session.is_running:
            self.practice_session.is_running = False

        self._clear_ref_state()
        self.db.upsert_game(game_id, game_name, self.default_category)
        self.game_id = game_id
        self.game_name = game_name
        self.scheduler = None
        self.mode = "idle"
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

        if evt_type == "rom_info":
            await self._handle_rom_info(event)
            return

        if evt_type == "game_context":
            gid = event.get("game_id")
            gname = event.get("game_name", gid or "unknown")
            if gid:
                await self.switch_game(gid, gname)
            return

        if evt_type == "level_entrance" and self.mode == "reference":
            key = event["level"]
            self.ref_pending[key] = event
            self.ref_pending_start = {
                "type": "entrance",
                "ordinal": 0,
                "state_path": event.get("state_path"),
                "timestamp_ms": 0,
                "level_num": event["level"],
            }
            self.ref_died = False
            await self._notify_sse()
            return

        if evt_type == "checkpoint" and self.mode == "reference":
            await self._handle_ref_checkpoint(event)
            return

        if evt_type == "death" and self.mode == "reference":
            self.ref_died = True
            return

        if evt_type == "spawn" and self.mode == "reference":
            await self._handle_ref_spawn(event)
            return

        if evt_type == "level_exit" and self.mode == "reference":
            await self._handle_ref_exit(event)
            return

        if evt_type == "attempt_result" and self.mode == "practice":
            if self.practice_session:
                self.practice_session.receive_result(event)
            await self._notify_sse()
            return

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

    async def _handle_ref_checkpoint(self, event: dict) -> None:
        """Handle checkpoint during reference: close current segment, start new one."""
        if not self.ref_pending_start:
            return

        gid = self._require_game()
        from .models import Segment, SegmentVariant

        start = self.ref_pending_start
        cp_ordinal = event.get("cp_ordinal", 1)
        level = event.get("level_num", start["level_num"])

        seg_id = Segment.make_id(
            gid, level,
            start["type"], start["ordinal"],
            "checkpoint", cp_ordinal,
        )
        self.ref_segments_count += 1
        seg = Segment(
            id=seg_id,
            game_id=gid,
            level_number=level,
            start_type=start["type"],
            start_ordinal=start["ordinal"],
            end_type="checkpoint",
            end_ordinal=cp_ordinal,
            ordinal=self.ref_segments_count,
            reference_id=self.ref_capture_run_id,
        )
        self.db.upsert_segment(seg)

        # Store hot variant (state captured mid-run, player has momentum)
        hot_path = event.get("state_path")
        if hot_path:
            self.db.add_variant(SegmentVariant(
                segment_id=seg_id,
                variant_type="hot",
                state_path=hot_path,
                is_default=False,
            ))

        # New pending start is this checkpoint
        self.ref_pending_start = {
            "type": "checkpoint",
            "ordinal": cp_ordinal,
            "state_path": hot_path,
            "timestamp_ms": event.get("timestamp_ms", 0),
            "level_num": level,
        }
        await self._notify_sse()

    async def _handle_ref_spawn(self, event: dict) -> None:
        """Handle spawn during reference: store cold variant if applicable."""
        if not event.get("is_cold_cp") or not event.get("state_captured"):
            return

        from .models import SegmentVariant

        # Cold variant goes on the segment that starts from this checkpoint
        # We need to find the segment that has this checkpoint as its start
        # The ref_pending_start should be this checkpoint
        if not self.ref_pending_start or self.ref_pending_start["type"] != "checkpoint":
            return

        cold_path = event.get("state_path")
        if not cold_path:
            return

        gid = self._require_game()
        # The cold variant is for the *next* segment starting from this checkpoint
        # We don't know the end yet, but we can store it keyed to the pending start info
        # For now, store it as a variant on the most recently created segment's start checkpoint
        # Actually, cold variants belong to segments that START at this checkpoint,
        # but that segment hasn't been created yet. We store it when the segment is created.
        # For simplicity, store it keyed to a synthetic segment ID pattern
        # that will match when the segment is eventually created.
        # TODO: revisit cold variant storage timing in Task 11
        logger.debug("Cold CP spawn: level=%s ordinal=%s path=%s",
                      event.get("level_num"), self.ref_pending_start.get("ordinal"), cold_path)

    async def _handle_ref_exit(self, event: dict) -> None:
        """Pair level_exit with pending start to create final segment."""
        key = event["level"]
        goal = event.get("goal", "abort")

        if goal == "abort":
            self.ref_pending.pop(key, None)
            self.ref_pending_start = None
            return

        entrance = self.ref_pending.pop(key, None)
        if not entrance or not self.ref_pending_start:
            return

        self.ref_segments_count += 1
        from .models import Segment, SegmentVariant
        gid = self._require_game()
        start = self.ref_pending_start

        # Map goal string to end_ordinal (0 for normal goals)
        end_ordinal = 0
        seg_id = Segment.make_id(
            gid, entrance["level"],
            start["type"], start["ordinal"],
            "goal", end_ordinal,
        )
        seg = Segment(
            id=seg_id,
            game_id=gid,
            level_number=entrance["level"],
            start_type=start["type"],
            start_ordinal=start["ordinal"],
            end_type="goal",
            end_ordinal=end_ordinal,
            description=goal,
            ordinal=self.ref_segments_count,
            reference_id=self.ref_capture_run_id,
        )
        self.db.upsert_segment(seg)

        # Store entrance variant for the segment
        state_path = start.get("state_path")
        if state_path:
            self.db.add_variant(SegmentVariant(
                segment_id=seg_id,
                variant_type="hot" if start["type"] == "checkpoint" else "cold",
                state_path=state_path,
                is_default=True,
            ))

        self.ref_pending_start = None
        await self._notify_sse()

    # --- Reference mode ---
    async def start_reference(self, run_name: str | None = None) -> dict:
        """Begin reference capture."""
        if self.mode == "practice":
            return {"status": "practice_active"}
        if not self.tcp.is_connected:
            return {"status": "not_connected"}
        gid = self._require_game()
        self._clear_ref_state()
        run_id = f"live_{uuid.uuid4().hex[:8]}"
        name = run_name or f"Live {datetime.now(UTC).strftime('%Y-%m-%d %H:%M')}"
        self.db.create_capture_run(run_id, gid, name)
        self.db.set_active_capture_run(run_id)
        self.ref_capture_run_id = run_id
        self.mode = "reference"
        await self._notify_sse()
        return {"status": "started", "run_id": run_id, "run_name": name}

    async def stop_reference(self) -> dict:
        """End reference capture."""
        if self.mode != "reference":
            return {"status": "not_in_reference"}
        self._clear_ref_state()
        await self._notify_sse()
        return {"status": "stopped"}

    # --- Practice mode ---
    async def start_practice(self) -> dict:
        """Begin practice session."""
        if self.practice_session and self.practice_session.is_running:
            return {"status": "already_running"}
        if not self.tcp.is_connected:
            return {"status": "not_connected"}
        if self.mode == "reference":
            self._clear_ref_state()

        from .practice import PracticeSession
        ps = PracticeSession(
            tcp=self.tcp, db=self.db, game_id=self._require_game(),
            on_attempt=lambda _: asyncio.ensure_future(self._notify_sse()),
        )
        self.practice_session = ps
        self.practice_task = asyncio.create_task(ps.run_loop())
        self.practice_task.add_done_callback(self._on_practice_done)
        self.mode = "practice"
        await self._notify_sse()
        return {"status": "started", "session_id": ps.session_id}

    def _on_practice_done(self, task: asyncio.Task) -> None:
        """Callback when practice task finishes."""
        if self.mode == "practice":
            self.mode = "idle"
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
            self.mode = "idle"
            await self._notify_sse()
            return {"status": "stopped"}
        if self.mode == "practice":
            self.mode = "idle"
            return {"status": "stopped"}
        return {"status": "not_running"}

    def on_disconnect(self) -> None:
        """Handle TCP disconnect: stop practice, clear ref state."""
        if self.practice_session and self.practice_session.is_running:
            self.practice_session.is_running = False
        self._clear_ref_state()

    async def shutdown(self) -> None:
        """Clean shutdown: stop sessions, close TCP."""
        await self.stop_practice()
        if self.mode == "reference":
            self._clear_ref_state()
        await self.tcp.disconnect()
