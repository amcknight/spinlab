"""SessionManager — thin coordinator that delegates to focused controllers."""
from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from .capture import ColdFillController, FillGapController, ReferenceController
from .errors import (
    AlreadyRunningError,
    DraftPendingError,
    MissingSaveStatesError,
    NotConnectedError,
    NotRunningError,
)
from .models import ActionResult, Mode, Status
from .protocol import (
    SPEED_UNCAPPED,
    AttemptInvalidatedEvent,
    AttemptResultEvent,
    CheckpointEvent,
    DeathEvent,
    GameContextEvent,
    LevelEntranceEvent,
    LevelExitEvent,
    ReplayErrorEvent,
    ReplayFinishedEvent,
    ReplayStartedEvent,
    ResetCmd,
    RomInfoEvent,
    SetConditionsCmd,
    SpawnEvent,
    SpeedRunCheckpointEvent,
    SpeedRunCompleteEvent,
    SpeedRunDeathEvent,
)
from .sse import SSEBroadcaster
from .state_builder import StateBuilder
from .system_state import SystemState

if TYPE_CHECKING:
    from .db import Database
    from .emu_backend import EmuBackend

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
        emu: "EmuBackend",
        rom_dir: Path | None,
        default_category: str = "any%",
        data_dir: Path | None = None,
        invalidate_combo: list[str] | None = None,
    ) -> None:
        self.db = db
        self.emu = emu
        self.rom_dir = rom_dir
        self.default_category = default_category
        self.data_dir = data_dir or Path("data")
        self.invalidate_combo: list[str] = invalidate_combo if invalidate_combo is not None else ["L", "Select"]

        self.state = SystemState()  # SystemState is the single source of truth
        self.scheduler = None  # Scheduler | None, lazy-init
        # FastAPI runs sync route handlers in a thread pool, so several requests
        # to /api/model/params and friends can race on the very first lazy-init
        # of Scheduler. Without this lock, two threads both see scheduler=None,
        # both call Scheduler(...), and both hit the shared db.conn concurrently
        # — which sqlite3 surfaces as InterfaceError: bad parameter or other API
        # misuse. Double-checked locking serializes the init while keeping the
        # cached fast path lock-free.
        self._scheduler_lock = threading.Lock()
        self.practice_session = None  # PracticeSession | None
        self.practice_task: asyncio.Task | None = None
        self.speed_run_session = None  # SpeedRunSession | None
        self.speed_run_task: asyncio.Task | None = None

        self.capture = ReferenceController(db, emu)
        self.cold_fill = ColdFillController(db, emu)
        self.fill_gap = FillGapController(db, emu)
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
            ReplayStartedEvent: self._handle_replay_started,
            ReplayFinishedEvent: self._handle_replay_finished,
            ReplayErrorEvent: self._handle_replay_error,
            AttemptInvalidatedEvent: self._handle_attempt_invalidated,
            SpeedRunCheckpointEvent: self._handle_speed_run_checkpoint,
            SpeedRunDeathEvent: self._handle_speed_run_death,
            SpeedRunCompleteEvent: self._handle_speed_run_complete,
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

    @property
    def current_session_id(self) -> str | None:
        """Session ID for the active practice or speed run, if any."""
        if self.mode == Mode.PRACTICE and self.practice_session:
            return self.practice_session.session_id
        if self.mode == Mode.SPEED_RUN and self.speed_run_session:
            return self.speed_run_session.session_id
        return None


    def get_state(self) -> dict:
        """Full state snapshot for API and SSE."""
        return self._state_builder.build(self)

    def get_scheduler(self):
        """Lazy-init scheduler for current game.

        Thread-safe: double-checked locking ensures Scheduler() runs exactly
        once even when FastAPI dispatches multiple route handlers in parallel
        worker threads.
        """
        if self.scheduler is None:
            with self._scheduler_lock:
                if self.scheduler is None:
                    from spinlab.scheduler import Scheduler
                    self.scheduler = Scheduler(self.db, self.require_game())
        return self.scheduler

    def require_game(self) -> str:
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
        self.capture.recover_paused_run(game_id)
        await self._notify_sse()


    def subscribe_sse(self) -> asyncio.Queue:
        return self.sse.subscribe()

    def unsubscribe_sse(self, queue: asyncio.Queue) -> None:
        self.sse.unsubscribe(queue)

    async def _notify_sse(self) -> None:
        """Push current state to SSE subscribers. Errors are swallowed so an
        unrelated SSE/state-builder failure cannot fail the action that
        triggered the broadcast — subscribers self-heal on the next event."""
        if not self.sse.has_subscribers:
            return
        try:
            await self.sse.broadcast(self.get_state())
        except Exception:
            logger.exception("SSE broadcast failed; subscribers will sync on next event")


    async def route_event(self, event: object) -> None:
        handler = self._event_handlers.get(type(event))
        if handler is None:
            logger.warning("No handler for event type %r: %r", type(event).__name__, event)
            return
        logger.info("event: %s (mode=%s)", type(event).__name__, self.mode.value)
        await handler(event)

    async def _handle_rom_info(self, event: RomInfoEvent) -> None:
        filename = event.filename
        if not self.rom_dir or not filename:
            return
        rom_path = self.rom_dir / filename
        if rom_path.exists():
            from spinlab.romid import game_name_from_filename, rom_checksum
            checksum = rom_checksum(rom_path)
            name = game_name_from_filename(filename)
        else:
            from spinlab.romid import game_name_from_filename
            name = game_name_from_filename(filename)
            checksum = f"file_{name.lower().replace(' ', '_')}"
            logger.warning("ROM not found in rom_dir: %s — using filename as ID", filename)
        await self.switch_game(checksum, name)
        await self.install_condition_registry(checksum)

    async def install_condition_registry(self, game_id: str) -> None:
        """Load per-game condition definitions and push them to the backend."""
        from .condition_registry import load_registry_for_game
        registry = load_registry_for_game(game_id)
        self.capture.set_condition_registry(registry)
        if self.emu.is_connected and registry.definitions:
            defs_payload = [
                {"name": d.name, "address": d.address, "size": d.size}
                for d in registry.definitions
            ]
            await self.emu.send_command(SetConditionsCmd(definitions=defs_payload))

    async def _handle_game_context(self, event: GameContextEvent) -> None:
        gid = event.game_id
        gname = event.game_name or gid or "unknown"
        if gid:
            await self.switch_game(gid, gname)

    async def _handle_level_entrance(self, event: LevelEntranceEvent) -> None:
        if not self.capture.is_recording:
            return
        await self.capture.handle_entrance(event)
        await self._notify_sse()

    async def _handle_checkpoint(self, event: CheckpointEvent) -> None:
        if not self.capture.is_recording:
            return
        await self.capture.handle_checkpoint(event, self.require_game())
        await self._notify_sse()

    async def _handle_death(self, event: DeathEvent) -> None:
        if self.mode == Mode.COLD_FILL:
            logger.info("death during cold_fill — waiting for respawn")
            return
        if self.capture.is_recording:
            self.capture.handle_death(event)
            return
        if self.mode == Mode.PRACTICE and self.practice_session:
            await self.practice_session.handle_death()

    async def _handle_spawn(self, event: SpawnEvent) -> None:
        if self.mode == Mode.COLD_FILL:
            done = await self.cold_fill.handle_spawn(event)
            if done:
                self.mode = Mode.IDLE
                # Power-cycle the emulator so the user lands at the title
                # screen instead of mid-respawn in whatever level the last
                # capture happened in.
                try:
                    await self.emu.send_command(ResetCmd())
                except (ConnectionError, OSError):
                    logger.warning("cold_fill: reset command failed (backend gone)")
            await self._notify_sse()
            return
        if self.mode == Mode.FILL_GAP:
            if self.fill_gap.handle_spawn(event):
                self.mode = Mode.IDLE
                await self._notify_sse()
            return
        if self.capture.is_recording:
            self.capture.handle_spawn(event, self.require_game())

    async def _handle_level_exit(self, event: LevelExitEvent) -> None:
        if self.mode == Mode.PRACTICE and self.practice_session and event.goal == "abort":
            # Pit-fall / death-fall — same reload semantics as a Death event.
            await self.practice_session.handle_level_exit_abort()
            return
        if not self.capture.is_recording:
            return
        self.capture.handle_exit(event, self.require_game())
        await self._notify_sse()

    async def _handle_attempt_result(self, event: AttemptResultEvent) -> None:
        if self.mode != Mode.PRACTICE:
            return
        if self.practice_session:
            self.practice_session.receive_result(event)
        await self._notify_sse()

    async def _handle_replay_started(self, event: ReplayStartedEvent) -> None:
        self.capture.handle_replay_started(event)
        await self._notify_sse()

    async def _handle_replay_finished(self, event: ReplayFinishedEvent) -> None:
        # handle_replay_finished ends the session and leaves the run paused if
        # segments were captured.  We must NOT call _clear_ref_and_idle here —
        # that would wipe paused_run_id and prevent the user from finalizing.
        self.capture.handle_replay_finished()
        self.mode = Mode.IDLE
        await self._notify_sse()

    async def _handle_replay_error(self, event: ReplayErrorEvent) -> None:
        logger.warning("replay_error: %s", event.message)
        # Same as _handle_replay_finished: preserve paused_run_id set by
        # handle_replay_error when segments were captured before the error.
        self.capture.handle_replay_error()
        self.mode = Mode.IDLE
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


    async def _apply_result(self, result: ActionResult) -> ActionResult:
        """Apply mode transition from result and notify SSE."""
        if result.new_mode is not None:
            self.mode = result.new_mode
        await self._notify_sse()
        return result

    async def start_reference(self, run_name: str | None = None) -> ActionResult:
        return await self._apply_result(
            await self.capture.start_reference(
                self.mode, self.require_game(), self.data_dir, run_name,
            )
        )

    async def stop_reference(self) -> ActionResult:
        return await self._apply_result(
            await self.capture.stop_reference(self.mode)
        )

    async def start_replay(self, spinrec_path: str, speed: int = SPEED_UNCAPPED) -> ActionResult:
        # capture.start_replay arms the recorder (is_recording=True) BEFORE
        # sending ReplayCmd, so any LevelEntrance the poller observes during
        # the send_command await reaches capture via the is_recording gate
        # in _handle_level_entrance — no eager mode flip needed.
        return await self._apply_result(
            await self.capture.start_replay(
                self.mode, self.require_game(), spinrec_path, speed,
            )
        )

    async def stop_replay(self) -> ActionResult:
        return await self._apply_result(
            await self.capture.stop_replay(self.mode)
        )

    async def start_fill_gap(self, segment_id: str) -> ActionResult:
        return await self._apply_result(
            await self.fill_gap.start(segment_id)
        )

    async def finalize_run(self, name: str) -> ActionResult:
        scheduler = self.get_scheduler() if self.game_id else None
        result = await self.capture.finalize_run(name, scheduler=scheduler)
        if result.status == Status.OK and self.game_id and self.emu.is_connected:
            cf_result = await self.cold_fill.start(self.game_id)
            if cf_result.new_mode == Mode.COLD_FILL:
                self.mode = Mode.COLD_FILL
        await self._notify_sse()
        return result

    async def save_and_finish_run(self, name: str) -> ActionResult:
        scheduler = self.get_scheduler() if self.game_id else None
        result = await self.capture.save_and_finish_run(self.mode, name, scheduler=scheduler)
        if result.new_mode is not None:
            self.mode = result.new_mode
        if result.status == Status.OK and self.game_id and self.emu.is_connected:
            cf_result = await self.cold_fill.start(self.game_id)
            if cf_result.new_mode == Mode.COLD_FILL:
                self.mode = Mode.COLD_FILL
        await self._notify_sse()
        return result

    async def discard_run(self) -> ActionResult:
        result = await self.capture.discard_run()
        await self._notify_sse()
        return result

    async def resume_reference(self) -> ActionResult:
        return await self._apply_result(
            await self.capture.resume_reference(
                self.mode, self.require_game(), self.data_dir,
            )
        )

    async def delete_capture_session(self, session_id: str) -> ActionResult:
        result = await self.capture.delete_capture_session(session_id)
        await self._notify_sse()
        return result


    async def start_practice(self) -> ActionResult:
        if self.capture.has_paused_run:
            raise DraftPendingError()
        if self.practice_session and self.practice_session.is_running:
            raise AlreadyRunningError()
        if not self.emu.is_connected:
            raise NotConnectedError()
        if self.mode == Mode.REFERENCE:
            self._clear_ref_and_idle()

        from .practice import PracticeSession
        ps = PracticeSession(
            emu=self.emu, db=self.db, game_id=self.require_game(),
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
        raise NotRunningError()


    async def start_speed_run(self) -> ActionResult:
        if self.capture.has_paused_run:
            raise DraftPendingError()
        if self.speed_run_session and self.speed_run_session.is_running:
            raise AlreadyRunningError()
        if not self.emu.is_connected:
            raise NotConnectedError()
        if self.mode == Mode.REFERENCE:
            self._clear_ref_and_idle()

        from .speed_run import SpeedRunSession
        try:
            sr = SpeedRunSession(
                emu=self.emu, db=self.db, game_id=self.require_game(),
                on_event=lambda _: asyncio.ensure_future(self._notify_sse()),
            )
        except ValueError:
            raise MissingSaveStatesError()

        self.speed_run_session = sr
        self.speed_run_task = asyncio.create_task(sr.run_loop())
        self.speed_run_task.add_done_callback(self._on_speed_run_done)
        self.mode = Mode.SPEED_RUN
        await self._notify_sse()
        return ActionResult(status=Status.STARTED, session_id=sr.session_id)

    def _on_speed_run_done(self, task: asyncio.Task) -> None:
        if self.mode == Mode.SPEED_RUN:
            self.mode = Mode.IDLE
            asyncio.ensure_future(self._notify_sse())

    async def stop_speed_run(self) -> ActionResult:
        if self.speed_run_session and self.speed_run_session.is_running:
            self.speed_run_session.is_running = False
            if self.speed_run_task:
                try:
                    await asyncio.wait_for(self.speed_run_task, timeout=PRACTICE_STOP_TIMEOUT_S)
                except asyncio.TimeoutError:
                    self.speed_run_task.cancel()
            self.mode = Mode.IDLE
            await self._notify_sse()
            return ActionResult(status=Status.STOPPED)
        if self.mode == Mode.SPEED_RUN:
            self.mode = Mode.IDLE
            return ActionResult(status=Status.STOPPED)
        raise NotRunningError()

    async def _handle_speed_run_checkpoint(self, event: SpeedRunCheckpointEvent) -> None:
        if self.mode != Mode.SPEED_RUN or not self.speed_run_session:
            return
        self.speed_run_session.receive_checkpoint(event)
        await self._notify_sse()

    async def _handle_speed_run_death(self, event: SpeedRunDeathEvent) -> None:
        if self.mode != Mode.SPEED_RUN or not self.speed_run_session:
            return
        self.speed_run_session.receive_death(event)
        await self._notify_sse()

    async def _handle_speed_run_complete(self, event: SpeedRunCompleteEvent) -> None:
        if self.mode != Mode.SPEED_RUN or not self.speed_run_session:
            return
        self.speed_run_session.receive_complete(event)
        await self._notify_sse()

    def on_disconnect(self) -> None:
        if self.practice_session and self.practice_session.is_running:
            self.practice_session.is_running = False
        if self.speed_run_session and self.speed_run_session.is_running:
            self.speed_run_session.is_running = False
        self.cold_fill.clear()
        self.capture.handle_disconnect()
        self._clear_ref_and_idle()

    async def shutdown(self) -> None:
        from .errors import NotRunningError
        try:
            await self.stop_practice()
        except NotRunningError:
            pass
        try:
            await self.stop_speed_run()
        except NotRunningError:
            pass
        if self.mode == Mode.REFERENCE:
            self._clear_ref_and_idle()
        await self.emu.disconnect()
