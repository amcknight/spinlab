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
        self.mode: str = "idle"  # "idle" | "reference" | "practice"
        self.game_id: str | None = None
        self.game_name: str | None = None
        self.scheduler = None  # Scheduler | None, lazy-init
        self.practice_session = None  # PracticeSession | None
        self.practice_task: asyncio.Task | None = None

        # Reference capture state
        self.ref_segments_count: int = 0
        self.ref_capture_run_id: str | None = None
        self.ref_pending_start: dict | None = None
        self.ref_died: bool = False
        self.rec_path: str | None = None

        # Fill-gap state
        self.fill_gap_segment_id: str | None = None

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

        if self.mode == "replay":
            base["replay"] = {"rec_path": self.rec_path}

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
        self.ref_segments_count = 0
        self.ref_capture_run_id = None
        self.ref_pending_start = None
        self.ref_died = False
        self.rec_path = None
        self.mode = "idle"

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

        if evt_type == "level_entrance" and self.mode in ("reference", "replay"):
            # Only set new pending start if we don't already have a checkpoint
            # pending.  SMW's level_start ($1935) can fire spuriously during
            # goal sequences; overwriting a checkpoint pending_start with an
            # entrance would create wrong segment types.
            if self.ref_pending_start and self.ref_pending_start["type"] != "entrance":
                logger.info("Ignoring level_entrance — pending start exists: %s", self.ref_pending_start)
                return
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

        if evt_type == "checkpoint" and self.mode in ("reference", "replay"):
            await self._handle_ref_checkpoint(event)
            return

        if evt_type == "death" and self.mode in ("reference", "replay"):
            self.ref_died = True
            return

        if evt_type == "spawn" and self.mode == "fill_gap":
            await self._handle_fill_gap_spawn(event)
            return

        if evt_type == "spawn" and self.mode in ("reference", "replay"):
            await self._handle_ref_spawn(event)
            return

        if evt_type == "level_exit" and self.mode in ("reference", "replay"):
            logger.info("level_exit: ref_pending_start=%s", self.ref_pending_start)
            await self._handle_ref_exit(event)
            return

        if evt_type == "attempt_result" and self.mode == "practice":
            if self.practice_session:
                self.practice_session.receive_result(event)
            await self._notify_sse()
            return

        if evt_type == "rec_saved":
            self.rec_path = event.get("path")
            return

        if evt_type == "replay_started":
            await self._notify_sse()
            return
        if evt_type == "replay_progress":
            await self._notify_sse()
            return
        if evt_type == "replay_finished":
            self._clear_ref_state()
            await self._notify_sse()
            return
        if evt_type == "replay_error":
            self._clear_ref_state()
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

        # Store the start variant — what practice mode loads for this segment.
        # For entrance→cp: the entrance state (cold).
        # For cp→cp: the previous checkpoint's hot state.
        start_path = start.get("state_path")
        if start_path:
            self.db.add_variant(SegmentVariant(
                segment_id=seg_id,
                variant_type="cold" if start["type"] == "entrance" else "hot",
                state_path=start_path,
                is_default=True,
            ))

        # New pending start is this checkpoint.
        # The state_path is the hot CP save state — it's what gets loaded
        # if this CP is the start of the next segment (cp→goal or cp→cp).
        self.ref_pending_start = {
            "type": "checkpoint",
            "ordinal": cp_ordinal,
            "state_path": event.get("state_path"),
            "timestamp_ms": event.get("timestamp_ms", 0),
            "level_num": level,
        }
        await self._notify_sse()

    async def _handle_ref_spawn(self, event: dict) -> None:
        """Handle spawn during reference: store cold variant if applicable."""
        if not event.get("is_cold_cp") or not event.get("state_captured"):
            return

        from .models import SegmentVariant

        cold_path = event.get("state_path")
        if not cold_path:
            return

        # Find segments starting at this checkpoint in the current level
        level = event.get("level_num")
        cp_ord = event.get("cp_ordinal")
        if level is None or cp_ord is None:
            return

        gid = self._require_game()
        segments = self.db.get_active_segments(gid)
        for seg in segments:
            if (seg.level_number == level and seg.start_type == "checkpoint"
                    and seg.start_ordinal == cp_ord):
                variant = SegmentVariant(
                    segment_id=seg.id,
                    variant_type="cold",
                    state_path=cold_path,
                    is_default=True,
                )
                self.db.add_variant(variant)
                logger.debug("Stored cold variant for segment %s: %s", seg.id, cold_path)
                break

    async def _handle_ref_exit(self, event: dict) -> None:
        """Pair level_exit with pending start to create final segment.

        Uses sequential pairing via ref_pending_start, NOT level_num keying.
        SMW's level_num ($13BF) is stale when level_start ($1935) fires, so
        entrance and exit events have different level numbers.
        """
        goal = event.get("goal", "abort")

        if goal == "abort":
            self.ref_pending_start = None
            return

        if not self.ref_pending_start:
            return

        self.ref_segments_count += 1
        from .models import Segment, SegmentVariant
        gid = self._require_game()
        start = self.ref_pending_start
        # Use exit event's level_num — it has the correct value
        level = event["level"]

        # Map goal string to end_ordinal (0 for normal goals)
        end_ordinal = 0
        seg_id = Segment.make_id(
            gid, level,
            start["type"], start["ordinal"],
            "goal", end_ordinal,
        )
        seg = Segment(
            id=seg_id,
            game_id=gid,
            level_number=level,
            start_type=start["type"],
            start_ordinal=start["ordinal"],
            end_type="goal",
            end_ordinal=end_ordinal,
            description="",  # auto-generated label used when empty
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

    # --- Fill-gap mode ---
    async def start_fill_gap(self, segment_id: str) -> dict:
        """Enter fill-gap mode: load hot variant so user can die for cold capture."""
        if not self.tcp.is_connected:
            return {"status": "not_connected"}

        hot = self.db.get_variant(segment_id, "hot")
        if not hot:
            return {"status": "no_hot_variant"}

        self.fill_gap_segment_id = segment_id
        self.mode = "fill_gap"
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
        self.mode = "idle"
        await self._notify_sse()

    # --- Reference mode ---
    async def start_reference(self, run_name: str | None = None) -> dict:
        """Begin reference capture."""
        if self.mode in ("practice", "replay"):
            return {"status": f"{self.mode}_active"}
        if not self.tcp.is_connected:
            return {"status": "not_connected"}
        gid = self._require_game()
        self._clear_ref_state()
        run_id = f"live_{uuid.uuid4().hex[:8]}"
        name = run_name or f"Live {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M')}"
        self.db.create_capture_run(run_id, gid, name)
        self.db.set_active_capture_run(run_id)
        self.ref_capture_run_id = run_id
        self.mode = "reference"
        rec_path = str(self._game_rec_dir() / f"{run_id}.spinrec")
        await self.tcp.send(json.dumps({"event": "reference_start", "path": rec_path}))
        await self._notify_sse()
        return {"status": "started", "run_id": run_id, "run_name": name}

    async def stop_reference(self) -> dict:
        """End reference capture."""
        if self.mode != "reference":
            return {"status": "not_in_reference"}
        if self.tcp.is_connected:
            await self.tcp.send(json.dumps({"event": "reference_stop"}))
        self._clear_ref_state()
        await self._notify_sse()
        return {"status": "stopped"}

    # --- Replay mode ---
    async def start_replay(self, spinrec_path: str, speed: int = 0) -> dict:
        """Begin replay of a .spinrec file."""
        if self.mode == "practice":
            return {"status": "practice_active"}
        if self.mode == "reference":
            return {"status": "reference_active"}
        if self.mode == "replay":
            return {"status": "already_replaying"}
        if not self.tcp.is_connected:
            return {"status": "not_connected"}

        # Set up reference capture so replayed events create segments
        gid = self._require_game()
        self._clear_ref_state()
        run_id = f"replay_{uuid.uuid4().hex[:8]}"
        name = f"Replay {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M')}"
        self.db.create_capture_run(run_id, gid, name)
        self.db.set_active_capture_run(run_id)
        self.ref_capture_run_id = run_id

        self.mode = "replay"
        await self.tcp.send(json.dumps({"event": "replay", "path": spinrec_path, "speed": speed}))
        await self._notify_sse()
        return {"status": "started", "run_id": run_id}

    async def stop_replay(self) -> dict:
        """Abort replay."""
        if self.mode != "replay":
            return {"status": "not_replaying"}
        if self.tcp.is_connected:
            await self.tcp.send(json.dumps({"event": "replay_stop"}))
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
