# python/spinlab/session_manager.py
"""SessionManager — thin coordinator that delegates to focused controllers."""
from __future__ import annotations

import asyncio
import dataclasses
import logging
from pathlib import Path
from collections.abc import Callable
from typing import TYPE_CHECKING

from .models import ActionResult, Mode, Status
from .protocol import (
    parse_event,
    RomInfoEvent, GameContextEvent, LevelEntranceEvent, CheckpointEvent,
    DeathEvent, SpawnEvent, LevelExitEvent, AttemptResultEvent,
    RecSavedEvent, ReplayStartedEvent, ReplayProgressEvent,
    ReplayFinishedEvent, ReplayErrorEvent, AttemptInvalidatedEvent,
    GameContextCmd, SetConditionsCmd, SetInvalidateComboCmd,
)
from .capture_controller import CaptureController
from .cold_fill_controller import ColdFillController
from .sse import SSEBroadcaster
from .state_builder import StateBuilder
from .system_state import SystemState

if TYPE_CHECKING:
    from .db import Database
    from .tcp_manager import TcpManager

logger = logging.getLogger(__name__)

PRACTICE_STOP_TIMEOUT_S = 5


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
        invalidate_combo: list[str] | None = None,
    ) -> None:
        self.db = db
        self.tcp = tcp
        self.rom_dir = rom_dir
        self.default_category = default_category
        self.data_dir = data_dir or Path("data")
        self.invalidate_combo: list[str] = invalidate_combo if invalidate_combo is not None else ["L", "Select"]

        # Core state — SystemState is the single source of truth
        self.state = SystemState()
        self.scheduler = None  # Scheduler | None, lazy-init
        self.practice_session = None  # PracticeSession | None
        self.practice_task: asyncio.Task | None = None

        # Delegated components
        self.capture = CaptureController(db, tcp)
        self.cold_fill = ColdFillController(db, tcp)
        self.sse = SSEBroadcaster()
        self._state_builder = StateBuilder(db)

        # Event dispatch table — keyed by event dataclass type
        self._event_handlers: dict[type, Callable] = {
            RomInfoEvent: self._handle_rom_info,
            GameContextEvent: self._handle_game_context,
            LevelEntranceEvent: self._handle_level_entrance,
            CheckpointEvent: self._handle_checkpoint,
            DeathEvent: self._handle_death,
            SpawnEvent: self._handle_spawn,
            LevelExitEvent: self._handle_level_exit,
            AttemptResultEvent: self._handle_attempt_result,
            RecSavedEvent: self._handle_rec_saved,
            ReplayStartedEvent: self._handle_replay_started,
            ReplayProgressEvent: self._handle_replay_progress,
            ReplayFinishedEvent: self._handle_replay_finished,
            ReplayErrorEvent: self._handle_replay_error,
            AttemptInvalidatedEvent: self._handle_attempt_invalidated,
        }

    @property
    def mode(self) -> Mode:
        return self.state.mode

    @mode.setter
    def mode(self, value: Mode) -> None:
        old = self.state.mode
        if old != value:
            logger.info("mode: %s → %s", old.value, value.value)
        self.state.mode = value

    @property
    def game_id(self) -> str | None:
        return self.state.game_id

    @game_id.setter
    def game_id(self, value: str | None) -> None:
        self.state.game_id = value

    @property
    def game_name(self) -> str | None:
        return self.state.game_name

    @game_name.setter
    def game_name(self, value: str | None) -> None:
        self.state.game_name = value

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
        return self._state_builder.build(self)

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
        logger.info("game: loading %s (%s)", game_name, game_id)
        if self.practice_session and self.practice_session.is_running:
            self.practice_session.is_running = False
        self._clear_ref_and_idle()
        self.db.upsert_game(game_id, game_name, self.default_category)
        self.game_id = game_id
        self.game_name = game_name
        self.scheduler = None
        self.mode = Mode.IDLE
        self.capture.recover_draft(game_id)
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
        try:
            typed_event = parse_event(event)
        except ValueError:
            logger.warning("Unknown/malformed event from Lua: %r", event)
            return
        handler = self._event_handlers.get(type(typed_event))
        if handler:
            logger.info("event: %s (mode=%s)", type(typed_event).__name__, self.mode.value)
            await handler(typed_event)

    async def _handle_rom_info(self, event: RomInfoEvent) -> None:
        filename = event.filename
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
        await self._install_condition_registry(checksum)
        await self.tcp.send_command(GameContextCmd(game_id=checksum, game_name=name))

    async def _install_condition_registry(self, game_id: str) -> None:
        """Load per-game condition definitions and push them to Lua over TCP."""
        from .condition_registry import load_registry_for_game
        registry = load_registry_for_game(game_id)
        self.capture.set_condition_registry(registry)
        if self.tcp.is_connected:
            if registry.definitions:
                defs_payload = [
                    {"name": d.name, "address": d.address, "size": d.size}
                    for d in registry.definitions
                ]
                await self.tcp.send_command(SetConditionsCmd(definitions=defs_payload))
            await self.tcp.send_command(SetInvalidateComboCmd(combo=self.invalidate_combo))

    async def _handle_game_context(self, event: GameContextEvent) -> None:
        gid = event.game_id
        gname = event.game_name or gid or "unknown"
        if gid:
            await self.switch_game(gid, gname)

    async def _handle_level_entrance(self, event: LevelEntranceEvent) -> None:
        if self.mode not in (Mode.REFERENCE, Mode.REPLAY):
            return
        self.capture.handle_entrance(dataclasses.asdict(event))
        await self._notify_sse()

    async def _handle_checkpoint(self, event: CheckpointEvent) -> None:
        if self.mode not in (Mode.REFERENCE, Mode.REPLAY):
            return
        self.capture.handle_checkpoint(dataclasses.asdict(event), self._require_game())
        await self._notify_sse()

    async def _handle_death(self, event: DeathEvent) -> None:
        if self.mode not in (Mode.REFERENCE, Mode.REPLAY, Mode.COLD_FILL):
            return
        if self.mode == Mode.COLD_FILL:
            logger.info("death during cold_fill — waiting for respawn")
        if self.mode in (Mode.REFERENCE, Mode.REPLAY):
            self.capture.handle_death(dataclasses.asdict(event))

    async def _handle_spawn(self, event: SpawnEvent) -> None:
        event_dict = dataclasses.asdict(event)
        if self.mode == Mode.COLD_FILL:
            done = await self.cold_fill.handle_spawn(event_dict)
            if done:
                self.mode = Mode.IDLE
            await self._notify_sse()
            return
        if self.mode == Mode.FILL_GAP:
            if self.capture.handle_fill_gap_spawn(event_dict):
                self.mode = Mode.IDLE
                await self._notify_sse()
            return
        if self.mode in (Mode.REFERENCE, Mode.REPLAY):
            self.capture.handle_spawn(event_dict, self._require_game())

    async def _handle_level_exit(self, event: LevelExitEvent) -> None:
        if self.mode not in (Mode.REFERENCE, Mode.REPLAY):
            return
        self.capture.handle_exit(dataclasses.asdict(event), self._require_game())
        await self._notify_sse()

    async def _handle_attempt_result(self, event: AttemptResultEvent) -> None:
        if self.mode != Mode.PRACTICE:
            return
        if self.practice_session:
            self.practice_session.receive_result(dataclasses.asdict(event))
        await self._notify_sse()

    async def _handle_rec_saved(self, event: RecSavedEvent) -> None:
        self.capture.handle_rec_saved(dataclasses.asdict(event))

    async def _handle_replay_started(self, event: ReplayStartedEvent) -> None:
        await self._notify_sse()

    async def _handle_replay_progress(self, event: ReplayProgressEvent) -> None:
        await self._notify_sse()

    async def _handle_replay_finished(self, event: ReplayFinishedEvent) -> None:
        self.capture.handle_replay_finished()
        self._clear_ref_and_idle()
        await self._notify_sse()

    async def _handle_replay_error(self, event: ReplayErrorEvent) -> None:
        self.capture.handle_replay_error()
        self._clear_ref_and_idle()
        await self._notify_sse()

    async def _handle_attempt_invalidated(self, event: AttemptInvalidatedEvent) -> None:
        """Mark the most recent practice attempt for the current session as invalidated."""
        if self.practice_session is None:
            return
        sid = self.practice_session.session_id
        aid = self.db.get_last_practice_attempt(session_id=sid)
        if aid is None:
            return
        self.db.set_attempt_invalidated(aid, True)
        logger.info("Marked attempt %d as invalidated", aid)

    # --- Mode actions (delegate to controllers, apply mode transitions) ---

    async def _apply_result(self, result: ActionResult) -> ActionResult:
        """Apply mode transition from result and notify SSE."""
        if result.new_mode is not None:
            self.mode = result.new_mode
        await self._notify_sse()
        return result

    async def start_reference(self, run_name: str | None = None) -> ActionResult:
        return await self._apply_result(
            await self.capture.start_reference(
                self.mode, self._require_game(), self.data_dir, run_name,
            )
        )

    async def stop_reference(self) -> ActionResult:
        return await self._apply_result(
            await self.capture.stop_reference(self.mode)
        )

    async def start_replay(self, spinrec_path: str, speed: int = 0) -> ActionResult:
        return await self._apply_result(
            await self.capture.start_replay(
                self.mode, self._require_game(), spinrec_path, speed,
            )
        )

    async def stop_replay(self) -> ActionResult:
        return await self._apply_result(
            await self.capture.stop_replay(self.mode)
        )

    async def start_fill_gap(self, segment_id: str) -> ActionResult:
        return await self._apply_result(
            await self.capture.start_fill_gap(segment_id)
        )

    async def save_draft(self, name: str) -> ActionResult:
        scheduler = self._get_scheduler() if self.game_id else None
        result = await self.capture.save_draft(name, scheduler=scheduler)
        if result.status == Status.OK and self.game_id and self.tcp.is_connected:
            cf_result = await self.cold_fill.start(self.game_id)
            if cf_result.new_mode == Mode.COLD_FILL:
                self.mode = Mode.COLD_FILL
        await self._notify_sse()
        return result

    async def discard_draft(self) -> ActionResult:
        result = await self.capture.discard_draft()
        await self._notify_sse()
        return result

    # --- Practice mode ---

    async def start_practice(self) -> ActionResult:
        if self.capture.has_draft:
            return ActionResult(status=Status.DRAFT_PENDING)
        if self.practice_session and self.practice_session.is_running:
            return ActionResult(status=Status.ALREADY_RUNNING)
        if not self.tcp.is_connected:
            return ActionResult(status=Status.NOT_CONNECTED)
        if self.mode == Mode.REFERENCE:
            self._clear_ref_and_idle()

        from .practice import PracticeSession
        ps = PracticeSession(
            tcp=self.tcp, db=self.db, game_id=self._require_game(),
            death_penalty_ms=self.capture.condition_registry.death_penalty_ms,
            on_attempt=lambda _: asyncio.ensure_future(self._notify_sse()),
        )
        self.practice_session = ps
        self.practice_task = asyncio.create_task(ps.run_loop())
        self.practice_task.add_done_callback(self._on_practice_done)
        self.mode = Mode.PRACTICE
        await self._notify_sse()
        return ActionResult(status=Status.STARTED, session_id=ps.session_id)

    def _on_practice_done(self, task: asyncio.Task) -> None:
        if self.mode == Mode.PRACTICE:
            self.mode = Mode.IDLE
            asyncio.ensure_future(self._notify_sse())

    async def stop_practice(self) -> ActionResult:
        if self.practice_session and self.practice_session.is_running:
            self.practice_session.is_running = False
            if self.practice_task:
                try:
                    await asyncio.wait_for(self.practice_task, timeout=PRACTICE_STOP_TIMEOUT_S)
                except asyncio.TimeoutError:
                    self.practice_task.cancel()
            self.mode = Mode.IDLE
            await self._notify_sse()
            return ActionResult(status=Status.STOPPED)
        if self.mode == Mode.PRACTICE:
            self.mode = Mode.IDLE
            return ActionResult(status=Status.STOPPED)
        return ActionResult(status=Status.NOT_RUNNING)

    def on_disconnect(self) -> None:
        if self.practice_session and self.practice_session.is_running:
            self.practice_session.is_running = False
        self.cold_fill.clear()
        self.capture.handle_disconnect()
        self._clear_ref_and_idle()

    async def shutdown(self) -> None:
        await self.stop_practice()
        if self.mode == Mode.REFERENCE:
            self._clear_ref_and_idle()
        await self.tcp.disconnect()
