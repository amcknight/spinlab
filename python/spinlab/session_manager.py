"""SessionManager — thin coordinator that delegates to focused controllers."""
from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from spinlab import log

from .capture import ColdFillController, FillGapController, ReferenceController
from .errors import (
    AlreadyRunningError,
    DraftPendingError,
    MissingSaveStatesError,
    NoGameLoadedError,
    NotConnectedError,
    NotRunningError,
    WrongModeError,
)
from .models import ActionResult, Mode, Status
from .protocol import (
    SPEED_UNCAPPED,
    AttemptInvalidatedEvent,
    AttemptResultEvent,
    CheckpointEvent,
    ConditionSpec,
    ControllerCommandEvent,
    ControllerMenuArmedEvent,
    DeathEvent,
    EventAttemptEmission,
    GameContextEvent,
    HyperPlayCheckpointEvent,
    HyperPlayCompleteEvent,
    HyperPlayDeathEvent,
    LevelEntranceEvent,
    LevelExitEvent,
    ReplayErrorEvent,
    ReplayFinishedEvent,
    ReplayStartedEvent,
    ResetCmd,
    RomInfoEvent,
    SetConditionsCmd,
    SpawnEvent,
)
from .sse import SSEBroadcaster
from .state_builder import StateBuilder
from .system_state import SystemState

if TYPE_CHECKING:
    from .db import Database
    from .emu_backend import EmuBackend
    from .scheduler import Scheduler

logger = logging.getLogger(__name__)


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
        *,
        practice_engine_rollouts: int | None = None,
    ) -> None:
        self.db = db
        self.emu = emu
        self.rom_dir = rom_dir
        self.default_category = default_category
        self.data_dir = data_dir or Path("data")
        self._practice_engine_rollouts = practice_engine_rollouts

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
        self.hyper_play_session = None  # HyperPlaySession | None
        self.hyper_play_task: asyncio.Task | None = None

        # Practice-session snapshot — captured at practice/hyper-play start to
        # anchor the live view's session diffs. A clean stop FREEZES it (stamps
        # ended_at) so the idle view persists; crash and game-switch clear it.
        self.practice_session_snapshot = None  # SessionSnapshot | None

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
            EventAttemptEmission: self._handle_event_attempt_emission,
            ReplayStartedEvent: self._handle_replay_started,
            ReplayFinishedEvent: self._handle_replay_finished,
            ReplayErrorEvent: self._handle_replay_error,
            AttemptInvalidatedEvent: self._handle_attempt_invalidated,
            ControllerCommandEvent: self._handle_controller_command,
            ControllerMenuArmedEvent: self._handle_controller_menu_armed,
            HyperPlayCheckpointEvent: self._handle_hyper_play_checkpoint,
            HyperPlayDeathEvent: self._handle_hyper_play_death,
            HyperPlayCompleteEvent: self._handle_hyper_play_complete,
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
        """Session ID for the active practice or hyper play run, if any."""
        if self.mode == Mode.PRACTICE and self.practice_session:
            return self.practice_session.session_id
        if self.mode == Mode.HYPER_PLAY and self.hyper_play_session:
            return self.hyper_play_session.session_id
        return None


    def get_state(self) -> dict:
        """Full state snapshot for API and SSE."""
        return self._state_builder.build(self)

    def get_scheduler(self) -> Scheduler:
        """Lazy-init scheduler for current game.

        Thread-safe: double-checked locking ensures Scheduler() runs exactly
        once even when FastAPI dispatches multiple route handlers in parallel
        worker threads.
        """
        if self.scheduler is None:
            with self._scheduler_lock:
                if self.scheduler is None:
                    from spinlab.scheduler import Scheduler
                    self.scheduler = Scheduler(
                        self.db, self.require_game(),
                        practice_engine_rollouts=self._practice_engine_rollouts,
                    )
        return self.scheduler

    def require_game(self) -> str:
        if self.game_id is None:
            raise NoGameLoadedError()
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
        self._clear_session_snapshot()
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
        except Exception as exc:
            log.warn(
                logger, "SSE broadcast failed; subscribers will sync on next event",
                exc=exc, subscriber_count=self.sse.subscriber_count,
            )


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
        # RA's GET_STATUS reports the ROM basename without extension
        # (e.g. "Love Yourself", not "Love Yourself.smc"), so a literal
        # `rom_dir / filename` lookup misses. Probe common SNES ROM
        # extensions before falling back to the filename-derived ID —
        # without this, the per-game ConditionRegistry is never loaded
        # (`games/<crc>/conditions.yaml` doesn't exist under the
        # `file_<name>` fallback key), the poller emits no transition
        # events, and segment capture silently produces zero rows. This
        # was the root cause of the 2026-05-18 `test_replay_produces_
        # segments` intermittent failure where mode=replay but
        # sections_captured=0 for the full 120s timeout.
        rom_path = self.rom_dir / filename
        if not rom_path.exists():
            for ext in (".smc", ".sfc", ".fig", ".swc"):
                candidate = self.rom_dir / f"{filename}{ext}"
                if candidate.exists():
                    rom_path = candidate
                    break
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
                ConditionSpec(name=d.name, address=d.address, size=d.size)
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
            return
        logger.info(
            "death event unhandled: mode=%s practice_session=%s",
            self.mode.value, bool(self.practice_session),
        )

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

    async def _handle_event_attempt_emission(self, event: EventAttemptEmission) -> None:
        """Persist a per-event row mid-attempt.

        Fires before the closing ``AttemptResultEvent`` for each died/survived
        event. The session stamps in session_id + source + chosen_allocator
        and writes one row through ``db.log_event_attempt``.

        Hyper Play mode is intentionally not wired: HyperPlayTiming's per-
        sub-segment semantics don't map 1:1 onto a single armed attempt,
        and the existing ``HyperPlaySession._record_attempt`` path already
        produces deaths=0 episode rows that the legacy shim splits cleanly.
        Phase 1 (v07 wiring) will revisit hyper-play event emission.
        """
        if self.mode == Mode.PRACTICE and self.practice_session:
            self.practice_session.receive_event_attempt(event)

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

    async def _handle_controller_menu_armed(self, event: ControllerMenuArmedEvent) -> None:
        self.state.menu_armed = event.armed
        await self._notify_sse()

    async def _handle_controller_command(self, event: ControllerCommandEvent) -> None:
        if event.command != "pause":
            logger.warning("unknown controller command: %r", event.command)
            return
        # Practice-scoped: the input layer is mode-agnostic but pause only
        # applies to a practice attempt (spec — practice mode only for now).
        if self.mode == Mode.PRACTICE and self.practice_session:
            await self.practice_session.toggle_pause()
        await self._notify_sse()


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

    async def start_cold_fill(self) -> ActionResult:
        """Start the cold-fill capture loop for the current game.

        Routes call this directly; it owns the full transition (game-loaded
        check, current-mode check, active-run lookup, controller dispatch,
        mode flip, SSE broadcast). The route layer only translates
        ActionError → HTTPException via the boundary handler.
        """
        game_id = self.require_game()  # raises NoGameLoadedError if no game
        if self.mode != Mode.IDLE:
            raise WrongModeError(self.mode)
        run_id = self.db.get_active_capture_run(game_id)
        if run_id is None:
            # No active reference run; cold fill has nothing to fill. The
            # router maps this same condition to a 400 today; we surface it
            # as a mode-conflict-adjacent failure — but there's no
            # NoActiveRunError in the V5 hierarchy yet. Until one lands,
            # raise WrongModeError with a clear detail at the route boundary.
            #
            # NOTE: this branch could justify its own ActionError subclass
            # (NoActiveRunError(404)) in a follow-up; for now the route
            # surfaces "wrong_mode" via the ActionError handler.
            raise WrongModeError(self.mode)  # mode=IDLE, but no run to fill
        result = await self.cold_fill.start(game_id, run_id=run_id)
        if result.new_mode == Mode.COLD_FILL:
            self.mode = Mode.COLD_FILL
        await self._notify_sse()
        return result

    async def skip_cold_fill(self) -> ActionResult:
        result = await self.cold_fill.skip()
        # Apply whatever mode the skip resolved to (COLD_FILL when it advanced
        # to the next segment, IDLE when the queue drained). The NO_GAPS branch
        # has no new_mode — it means _load_next emptied the queue because every
        # remaining segment's hot state file was missing — so treat it as drain.
        if result.new_mode is not None:
            self.mode = result.new_mode
        elif result.status == Status.NO_GAPS:
            self.mode = Mode.IDLE
        await self._notify_sse()
        return result

    async def abort_cold_fill(self) -> ActionResult:
        self.cold_fill.abort()
        self.mode = Mode.IDLE
        await self._notify_sse()
        return ActionResult(status=Status.STOPPED, new_mode=Mode.IDLE)

    async def reset_data(self) -> None:
        """Reset all practice/reference data for the current game.

        Full sequence: stop practice (if running), clear reference state
        (if in REFERENCE mode), nuke the per-game DB rows, clear the
        cached scheduler, return to IDLE. Replaces the per-route mutation
        sequence that routes/system.py:reset_data used to drive directly.

        NOTE: this does not broadcast SSE — the caller (typically the
        /reset route) returns immediately and the user-driven action is
        complete. State pushes happen on the next event.
        """
        try:
            await self.stop_practice()
        except NotRunningError:
            pass
        if self.mode == Mode.REFERENCE:
            self._clear_ref_and_idle()
        gid = self.game_id
        if gid:
            logger.warning("reset: clearing all data for game=%s", gid)
            self.db.reset_game_data(gid)
        self.scheduler = None
        self.mode = Mode.IDLE

    async def finalize_run(self, name: str) -> ActionResult:
        scheduler = self.get_scheduler() if self.game_id else None
        result = await self.capture.finalize_run(name, scheduler=scheduler)
        await self._notify_sse()
        return result

    async def save_and_finish_run(self, name: str) -> ActionResult:
        scheduler = self.get_scheduler() if self.game_id else None
        result = await self.capture.save_and_finish_run(self.mode, name, scheduler=scheduler)
        if result.new_mode is not None:
            self.mode = result.new_mode
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


    def _snapshot_inputs(self):
        """Sequence of (seg_id, SamplerState, episodes) for every active segment.

        Called by _take_session_snapshot. Pulls per-segment SamplerStates from
        the scheduler's cached map; segments without a saved model_state row
        fall back to an empty SamplerState (which fails gate_passes and yields
        a None-baseline, matching the prior replay-of-empty-events behavior).
        Tests can override this method to bypass DB/scheduler plumbing.
        """
        from spinlab.estimators.em_suite_sampler import SamplerState

        if self.scheduler is None or self.state.game_id is None:
            return []
        # Scope the baseline to the active reference run's traversed segments.
        # No active run -> no segments to snapshot.
        active_run = self.db.get_active_capture_run(self.state.game_id)
        if active_run is None:
            return []
        cached = self.scheduler.sampler_states()
        out = []
        for seg in self.db.get_segments_for_run(self.state.game_id, active_run):
            state = cached.get(seg.id) or SamplerState()
            episodes = self.db.get_segment_attempts(seg.id)
            out.append((seg.id, state, episodes))
        return out

    def _take_session_snapshot(self) -> None:
        """Capture an in-memory baseline of every active segment + the route
        aggregate. Called from practice/hyper-play start.

        On failure: log WARNING, clear snapshot to None, re-raise so the
        caller (start_practice/start_hyper_play) can roll back the session.
        Silent failure would leave the live view emitting all-None diffs
        with no observable cause."""
        import time as _time

        from spinlab.estimators.session_snapshot import snapshot_from_segments

        try:
            inputs = self._snapshot_inputs()
            self.practice_session_snapshot = snapshot_from_segments(
                started_at=_time.time(),
                segments=inputs,
            )
            logger.info(
                "snapshot captured: n_segments=%d", len(inputs),
            )
        except Exception:
            self.practice_session_snapshot = None
            logger.warning("snapshot capture failed", exc_info=True)
            raise

    def _clear_session_snapshot(self) -> None:
        self.practice_session_snapshot = None

    def _freeze_session_snapshot(self) -> None:
        """Stamp the live snapshot's ended_at so it survives the stop transition
        for the idle 'frozen session' view. Idempotent; no-op when there is no
        snapshot or it is already frozen. Uses dataclasses.replace because
        SessionSnapshot is frozen."""
        import time as _time
        from dataclasses import replace

        snap = self.practice_session_snapshot
        if snap is None or snap.ended_at is not None:
            return
        frozen = replace(snap, ended_at=_time.time())
        self.practice_session_snapshot = frozen
        logger.info("snapshot frozen: ended_at=%.1f", frozen.ended_at)

    async def start_practice(self) -> ActionResult:
        from .errors import SnapshotFailedError

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
            scheduler=self.get_scheduler(),
            death_penalty_ms=self.capture.condition_registry.death_penalty_ms,
            on_attempt=lambda _: asyncio.create_task(self._notify_sse()),
            on_segment_load=lambda _: asyncio.create_task(self._notify_sse()),
        )
        self.practice_session = ps
        self.practice_task = asyncio.create_task(ps.run_loop())
        self.practice_task.add_done_callback(self._on_practice_done)
        self.mode = Mode.PRACTICE
        try:
            self._take_session_snapshot()
        except Exception as exc:
            # Roll back the half-started session so the caller can retry.
            # The done-callback would also clear mode eventually, but the
            # route needs a clean IDLE *now* and a typed error to surface.
            ps.is_running = False
            self.practice_task.cancel()
            self.practice_session = None
            self.practice_task = None
            self.mode = Mode.IDLE
            self._clear_session_snapshot()
            raise SnapshotFailedError() from exc
        await self._notify_sse()
        return ActionResult(status=Status.STARTED, session_id=ps.session_id)

    def _on_practice_done(self, task: asyncio.Task) -> None:
        clean = False
        if task.cancelled():
            pass  # abnormal teardown (rollback/cancel) — not a clean finish
        else:
            exc = task.exception()
            if exc is not None:
                logger.error("practice task crashed", exc_info=exc)
            else:
                clean = True
        if self.mode == Mode.PRACTICE:
            self.mode = Mode.IDLE
            # A clean finish freezes the snapshot so the idle view persists, the
            # same as a user stop; a crash/cancel clears it (no stale baseline).
            if clean:
                self._freeze_session_snapshot()
            else:
                self._clear_session_snapshot()
            asyncio.create_task(self._notify_sse())

    async def stop_practice(self) -> ActionResult:
        if self.practice_session and self.practice_session.is_running:
            self.practice_session.is_running = False
            # Don't await the task — run_loop cleans up (disarm, end_session) in
            # its finally block within one SEGMENT_LOAD_TIMEOUT_S cycle (~1s).
            # Awaiting it was the source of the UI lag.
            self.mode = Mode.IDLE
            self._freeze_session_snapshot()
            await self._notify_sse()
            return ActionResult(status=Status.STOPPED)
        if self.mode == Mode.PRACTICE:
            self.mode = Mode.IDLE
            self._freeze_session_snapshot()
            return ActionResult(status=Status.STOPPED)
        raise NotRunningError()

    async def invalidate_current_attempt(self) -> None:
        """Mark the current practice attempt as invalidated.

        Public entry point for the dashboard's invalidate button. Delegates
        to the same handler used by route_event(AttemptInvalidatedEvent) so
        the in-flight emu event path and the route path stay aligned.
        """
        await self._handle_attempt_invalidated(AttemptInvalidatedEvent())


    async def start_hyper_play(self) -> ActionResult:
        from .errors import SnapshotFailedError

        if self.capture.has_paused_run:
            raise DraftPendingError()
        if self.hyper_play_session and self.hyper_play_session.is_running:
            raise AlreadyRunningError()
        if not self.emu.is_connected:
            raise NotConnectedError()
        if self.mode == Mode.REFERENCE:
            self._clear_ref_and_idle()

        from .hyper_play import HyperPlaySession
        try:
            sr = HyperPlaySession(
                emu=self.emu, db=self.db, game_id=self.require_game(),
                on_event=lambda _: asyncio.create_task(self._notify_sse()),
            )
        except ValueError:
            raise MissingSaveStatesError()

        self.hyper_play_session = sr
        self.hyper_play_task = asyncio.create_task(sr.run_loop())
        self.hyper_play_task.add_done_callback(self._on_hyper_play_done)
        self.mode = Mode.HYPER_PLAY
        try:
            self._take_session_snapshot()
        except Exception as exc:
            sr.is_running = False
            self.hyper_play_task.cancel()
            self.hyper_play_session = None
            self.hyper_play_task = None
            self.mode = Mode.IDLE
            self._clear_session_snapshot()
            raise SnapshotFailedError() from exc
        await self._notify_sse()
        return ActionResult(status=Status.STARTED, session_id=sr.session_id)

    def _on_hyper_play_done(self, task: asyncio.Task) -> None:
        clean = False
        if task.cancelled():
            pass  # abnormal teardown (rollback/cancel) — not a clean finish
        else:
            exc = task.exception()
            if exc is not None:
                logger.error("hyper_play task crashed", exc_info=exc)
            else:
                clean = True
        if self.mode == Mode.HYPER_PLAY:
            self.mode = Mode.IDLE
            # Completing all levels ends the task cleanly while still HYPER_PLAY —
            # freeze so the finished session's view persists, like a user stop. A
            # crash/cancel clears it instead.
            if clean:
                self._freeze_session_snapshot()
            else:
                self._clear_session_snapshot()
            asyncio.create_task(self._notify_sse())

    async def stop_hyper_play(self) -> ActionResult:
        if self.hyper_play_session and self.hyper_play_session.is_running:
            self.hyper_play_session.is_running = False
            # Don't await the task — same rationale as stop_practice.
            self.mode = Mode.IDLE
            self._freeze_session_snapshot()
            await self._notify_sse()
            return ActionResult(status=Status.STOPPED)
        if self.mode == Mode.HYPER_PLAY:
            self.mode = Mode.IDLE
            self._freeze_session_snapshot()
            return ActionResult(status=Status.STOPPED)
        raise NotRunningError()

    async def _handle_hyper_play_checkpoint(self, event: HyperPlayCheckpointEvent) -> None:
        if self.mode != Mode.HYPER_PLAY or not self.hyper_play_session:
            return
        self.hyper_play_session.receive_checkpoint(event)
        await self._notify_sse()

    async def _handle_hyper_play_death(self, event: HyperPlayDeathEvent) -> None:
        if self.mode != Mode.HYPER_PLAY or not self.hyper_play_session:
            return
        self.hyper_play_session.receive_death(event)
        await self._notify_sse()

    async def _handle_hyper_play_complete(self, event: HyperPlayCompleteEvent) -> None:
        if self.mode != Mode.HYPER_PLAY or not self.hyper_play_session:
            return
        self.hyper_play_session.receive_complete(event)
        await self._notify_sse()

    def on_disconnect(self) -> None:
        if self.practice_session and self.practice_session.is_running:
            self.practice_session.is_running = False
        if self.hyper_play_session and self.hyper_play_session.is_running:
            self.hyper_play_session.is_running = False
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
            await self.stop_hyper_play()
        except NotRunningError:
            pass
        if self.mode == Mode.REFERENCE:
            self._clear_ref_and_idle()
        await self.emu.disconnect()
