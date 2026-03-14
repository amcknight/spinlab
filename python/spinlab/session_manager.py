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
        self.ref_splits_count: int = 0
        self.ref_capture_run_id: str | None = None

        # SSE subscribers
        self._sse_subscribers: list[asyncio.Queue] = []

    def get_state(self) -> dict:
        """Full state snapshot for API and SSE."""
        base = {
            "mode": self.mode,
            "tcp_connected": self.tcp.is_connected,
            "game_id": self.game_id,
            "game_name": self.game_name,
            "current_split": None,
            "queue": [],
            "recent": [],
            "session": None,
            "sections_captured": self.ref_splits_count,
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
                "splits_attempted": ps.splits_attempted,
                "splits_completed": ps.splits_completed,
            }
            if ps.current_split_id:
                splits = self.db.get_all_splits_with_model(self.game_id)
                split_map = {s["id"]: s for s in splits}
                if ps.current_split_id in split_map:
                    current_split = split_map[ps.current_split_id]
                    current_split["attempt_count"] = self.db.get_split_attempt_count(
                        ps.current_split_id, ps.session_id
                    )
                    model_row = self.db.load_model_state(ps.current_split_id)
                    if model_row and model_row["state_json"]:
                        from spinlab.estimators.kalman import KalmanState
                        from spinlab.estimators import get_estimator
                        state = KalmanState.from_dict(json.loads(model_row["state_json"]))
                        est = get_estimator(model_row["estimator"])
                        current_split["drift_info"] = est.drift_info(state)
                    base["current_split"] = current_split

            queue_ids = sched.peek_next_n(3)
            if ps.current_split_id:
                queue_ids = [q for q in queue_ids if q != ps.current_split_id][:2]
            splits_all = self.db.get_all_splits_with_model(self.game_id)
            smap = {s["id"]: s for s in splits_all}
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
        self.ref_splits_count = 0
        self.ref_capture_run_id = None
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
            key = (event["level"], event["room"])
            self.ref_pending[key] = event
            await self._notify_sse()
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

    async def _handle_ref_exit(self, event: dict) -> None:
        """Pair level_exit with pending entrance to create a split."""
        key = (event["level"], event["room"])
        goal = event.get("goal", "abort")

        if goal == "abort":
            self.ref_pending.pop(key, None)
            return

        entrance = self.ref_pending.pop(key, None)
        if not entrance:
            return

        self.ref_splits_count += 1
        from .models import Split
        gid = self._require_game()
        split_id = Split.make_id(gid, entrance["level"], entrance["room"], goal)
        split = Split(
            id=split_id,
            game_id=gid,
            level_number=entrance["level"],
            room_id=entrance["room"],
            goal=goal,
            state_path=entrance.get("state_path"),
            reference_time_ms=event.get("elapsed_ms"),
            ordinal=self.ref_splits_count,
            reference_id=self.ref_capture_run_id,
        )
        self.db.upsert_split(split)
        await self._notify_sse()

    async def shutdown(self) -> None:
        """Clean shutdown: stop sessions, close TCP."""
        await self.stop_practice()
        if self.mode == "reference":
            self._clear_ref_state()
        await self.tcp.disconnect()

    async def stop_practice(self) -> dict:
        """Stop practice session (stub — full impl in Task 3)."""
        return {"status": "not_running"}
