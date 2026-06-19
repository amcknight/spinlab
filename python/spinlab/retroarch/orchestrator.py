"""RetroArchOrchestrator — implements the EmuBackend protocol.

Owns RAClient + Poller + ConditionRegistry + timing modules. Translates typed
protocol commands into RAClient calls and publishes typed protocol events
into an asyncio.Queue consumed by ``session_manager.route_event``.

Stays thin: command dispatch + tick loop + event routing. All RA-specific
mechanics (NCI socket, save/load with mtime polling, movie record/play, slot
resolution, hotkey quirks) live in RAClient.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from pathlib import Path

from spinlab import log
from spinlab.condition_registry import ConditionRegistry
from spinlab.protocol import (
    ColdFillLoadCmd,
    FillGapLoadCmd,
    HyperPlayLoadCmd,
    HyperPlayStopCmd,
    PracticeLoadCmd,
    PracticePauseCmd,
    PracticeStopCmd,
    ReferenceStartCmd,
    ReferenceStopCmd,
    ReplayCmd,
    ReplayStopCmd,
    ResetCmd,
    RomInfoEvent,
    SetConditionsCmd,
)
from spinlab.retroarch.movies import MovieController
from spinlab.retroarch.raclient import (
    NotReachableError,
    RAClient,
)
from spinlab.state_paths import StatePathResolver
from spinlab.timing import HyperPlayTiming, PracticeTiming

logger = logging.getLogger(__name__)

# 20 Hz tick — fast enough for auto_advance_delay_ms precision without
# burning unnecessary CPU. The poller already runs at ~60 Hz; this only
# drives timing deadlines, not frame-by-frame detection.
TICK_INTERVAL_SEC = 0.05

# How often the tick loop re-checks RA's loaded ROM (backlog item B). Game
# switches are human-initiated and infrequent, so a coarse interval is plenty;
# it matches the dashboard's reconnect cadence and keeps the extra GET_STATUS
# traffic off the 60 Hz poller's (lock-shared) NCI socket down to a trickle.
ROM_CHECK_INTERVAL_SEC = 2.0


class RetroArchOrchestrator:
    """EmuBackend implementation over RAClient + Poller + timing modules.

    Components are duck-typed in tests; production wiring is done by
    ``build_orchestrator`` from AppConfig.
    """

    def __init__(
        self,
        *,
        raclient: RAClient,
        poller,
        conditions: ConditionRegistry,
        practice_timing: PracticeTiming,
        hyper_play_timing: HyperPlayTiming,
        state_paths: StatePathResolver,
        movies: MovieController,
        gamepad=None,
    ) -> None:
        self._raclient = raclient
        self._poller = poller
        self._conditions = conditions
        self._practice_timing = practice_timing
        self._hyper_play_timing = hyper_play_timing
        self._state_paths = state_paths
        self._movies = movies
        self._gamepad = gamepad

        # EmuBackend public surface
        self.events: asyncio.Queue[object] = asyncio.Queue()
        self.on_disconnect: Callable | None = None

        # Bind the movie controller's event callback to our queue now that
        # the queue exists. MovieController is constructed before orch
        # (because orch needs it) but emits events into orch.events.
        self._movies.set_event_callback(self.events.put_nowait)

        self._connected = False
        self._running = False
        self._poller_task: asyncio.Task | None = None
        self._tick_task: asyncio.Task | None = None

        # ROM basename last reported to the session, so the tick loop only
        # re-emits RomInfoEvent when RA actually swaps ROMs. See
        # ``_check_rom_change``. Seeded by ``connect()``.
        self._last_rom_basename: str | None = None

        # Suppress the "NCI not reachable" warning after the first one in a
        # disconnect streak. The dashboard's event_loop polls connect() every
        # 2s; without suppression, an idle dashboard with RA not yet launched
        # spams the log. Reset on successful connect.
        self._not_reachable_warning_logged = False

        self._dispatch: dict[type, Callable] = {
            PracticeLoadCmd: self._on_practice_load,
            PracticeStopCmd: self._on_practice_stop,
            PracticePauseCmd: self._on_practice_pause,
            HyperPlayLoadCmd: self._on_hyper_play_load,
            HyperPlayStopCmd: self._on_hyper_play_stop,
            ColdFillLoadCmd: self._on_cold_fill_load,
            FillGapLoadCmd: self._on_fill_gap_load,
            ResetCmd: self._on_reset,
            SetConditionsCmd: self._on_set_conditions,
            ReferenceStartCmd: self._on_reference_start,
            ReferenceStopCmd: self._on_reference_stop,
            ReplayCmd: self._on_replay,
            ReplayStopCmd: self._on_replay_stop,
        }

    # ------------------------------------------------------------------
    # EmuBackend public surface
    # ------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self, timeout: float = 5.0) -> bool:
        """Probe NCI, emit startup rom_info, start poller + tick loops."""
        try:
            info = await self._raclient.connect(timeout=timeout)
        except NotReachableError as exc:
            if not self._not_reachable_warning_logged:
                logger.warning(
                    "RetroArch NCI not reachable: %s — will keep retrying "
                    "every %ds (further failures suppressed until reconnect)",
                    exc, 2,
                )
                self._not_reachable_warning_logged = True
            else:
                logger.debug("RetroArch NCI still not reachable: %s", exc)
            return False

        # Race: NCI port opens before the core finishes loading the ROM.
        # GET_STATUS returns an empty `game` field in that window. Don't flip
        # to connected — the dashboard's 2s reconnect tick will retry once
        # the ROM is actually loaded, at which point `rom_filename` is set.
        if not info.rom_filename:
            logger.debug(
                "RetroArchOrchestrator: NCI reachable but no ROM loaded yet "
                "— will retry on next reconnect tick",
            )
            return False

        self._not_reachable_warning_logged = False
        self.events.put_nowait(RomInfoEvent(filename=info.rom_filename))
        self._last_rom_basename = info.rom_filename

        self._connected = True
        self._running = True
        self._poller_task = asyncio.create_task(self._poller.run())
        self._tick_task = asyncio.create_task(self._tick_loop())
        if self._gamepad is not None:
            self._gamepad.bind(
                loop=asyncio.get_running_loop(),
                on_event=self._enqueue,
            )
            self._gamepad.start()
        logger.info("RetroArchOrchestrator connected")
        return True

    async def disconnect(self) -> None:
        """Stop background tasks and close the NCI connection."""
        self._connected = False
        self._running = False

        if self._poller is not None:
            self._poller.stop()
        if self._poller_task is not None:
            try:
                await asyncio.wait_for(self._poller_task, timeout=1.0)
            except asyncio.TimeoutError:
                self._poller_task.cancel()
                try:
                    await self._poller_task
                except asyncio.CancelledError:
                    pass
            self._poller_task = None

        if self._gamepad is not None:
            self._gamepad.stop()

        if self._tick_task is not None:
            self._tick_task.cancel()
            try:
                await self._tick_task
            except asyncio.CancelledError:
                pass
            self._tick_task = None

        try:
            await self._raclient.disconnect()
        except Exception:
            logger.debug("disconnect: raclient.disconnect raised", exc_info=True)

        logger.info("RetroArchOrchestrator disconnected")

    async def save_state(self, segment_id: str) -> None:
        """EmuBackend Protocol method. Resolves segment_id to a path, then
        delegates to RAClient.
        """
        path = self._state_paths.path_for(segment_id)
        await self._raclient.save_state(path)

    async def load_state(self, state_path: str) -> None:
        """EmuBackend Protocol method. Path is taken verbatim; the poller
        learns about the load via RAClient's state_version counter.
        """
        await self._raclient.load_state(Path(state_path))

    async def recv_event(self, timeout: float | None = None) -> object | None:
        try:
            if timeout is not None:
                return await asyncio.wait_for(self.events.get(), timeout=timeout)
            return await self.events.get()
        except asyncio.TimeoutError:
            return None

    async def send_command(self, cmd) -> None:
        handler = self._dispatch.get(type(cmd))
        if handler is None:
            logger.warning(
                "RetroArchOrchestrator: ignoring unknown cmd type %r: %r",
                type(cmd).__name__,
                cmd,
            )
            return
        await handler(cmd)

    # ------------------------------------------------------------------
    # Command handlers
    # ------------------------------------------------------------------

    async def _on_practice_load(self, cmd: PracticeLoadCmd) -> None:
        await self.load_state(cmd.state_path)
        self._practice_timing.arm(
            segment_id=cmd.id,
            end_type=cmd.end_type,
            death_penalty_ms=cmd.death_penalty_ms,
            auto_advance_delay_ms=cmd.auto_advance_delay_ms,
            on_attempt_result=self._enqueue,
            on_event_attempt=self._enqueue,
        )

    async def _on_practice_stop(self, cmd: PracticeStopCmd) -> None:
        self._practice_timing.disarm()

    async def _on_practice_pause(self, cmd: PracticePauseCmd) -> None:
        # Pause drops the in-flight attempt by disarming timing; resume re-arms
        # via a fresh PracticeLoadCmd from PracticeSession.toggle_pause.
        self._practice_timing.disarm()

    async def _on_hyper_play_load(self, cmd: HyperPlayLoadCmd) -> None:
        await self.load_state(cmd.state_path)
        self._hyper_play_timing.arm(
            segment_id=cmd.id,
            checkpoints=list(cmd.checkpoints),
            auto_advance_delay_ms=cmd.auto_advance_delay_ms,
            death_delay_ms=cmd.death_delay_ms,
            on_event=self._enqueue,
        )

    async def _on_hyper_play_stop(self, cmd: HyperPlayStopCmd) -> None:
        self._hyper_play_timing.disarm()

    async def _on_cold_fill_load(self, cmd: ColdFillLoadCmd) -> None:
        await self.load_state(cmd.state_path)
        self._poller.activate_cold_fill(cmd.segment_id)
        logger.info(
            "cold_fill_load: state loaded and detector activated for segment=%s",
            cmd.segment_id,
        )

    async def _on_fill_gap_load(self, cmd: FillGapLoadCmd) -> None:
        await self.load_state(cmd.state_path)

    async def _on_reset(self, cmd: ResetCmd) -> None:
        await self._raclient.reset()

    async def _on_set_conditions(self, cmd: SetConditionsCmd) -> None:
        self._conditions.replace_with_read_specs(cmd.definitions)

    async def _on_reference_start(self, cmd: ReferenceStartCmd) -> None:
        await self._movies.start_recording(Path(cmd.path))

    async def _on_reference_stop(self, cmd: ReferenceStopCmd) -> None:
        await self._movies.stop_recording()

    async def _on_replay(self, cmd: ReplayCmd) -> None:
        await self._movies.start_playback(Path(cmd.path), cmd.speed)

    async def _on_replay_stop(self, cmd: ReplayStopCmd) -> None:
        await self._movies.stop_playback()

    # ------------------------------------------------------------------
    # Event plumbing
    # ------------------------------------------------------------------

    def on_poller_event(self, ev: object) -> None:
        """Sync callback for the poller's on_event. Feed timing modules + enqueue."""
        if self._practice_timing is not None:
            self._practice_timing.observe_event(ev)
        if self._hyper_play_timing is not None:
            self._hyper_play_timing.observe_event(ev)
        self.events.put_nowait(ev)

    def _enqueue(self, ev: object) -> None:
        self.events.put_nowait(ev)

    # ------------------------------------------------------------------
    # Background tasks
    # ------------------------------------------------------------------

    async def _check_rom_change(self) -> None:
        """Re-emit RomInfoEvent if RA has loaded a different ROM since the last
        check.

        RomInfoEvent is otherwise emitted only once, from ``connect()``. But RA
        can load a different ROM while we stay connected — the user switches
        games in RA's Quick Menu or relaunches RA — and the poller's
        socket-level reconnect never re-runs ``connect()``. Without this poll
        the dashboard stays stuck on the first ROM (backlog item B). The
        downstream ``switch_game`` is idempotent, so emitting only on an actual
        change keeps its checksum + condition-registry reload off the hot path.
        """
        try:
            status = await self._raclient.get_status()
        except Exception as exc:
            # RA may have died between checks; the poller's own reconnect path
            # handles socket recovery. A best-effort poll must not crash the
            # tick loop.
            logger.debug("rom-change check: get_status failed: %s", exc)
            return
        rom = status.game or ""
        if not rom or rom == self._last_rom_basename:
            return
        logger.info("rom-change detected: %r -> %r", self._last_rom_basename, rom)
        self._last_rom_basename = rom
        # Refresh RAClient's cached basename so movie record/replay staging uses
        # the live ROM name, not the stale one from connect() (backlog D #2).
        self._raclient.set_game_basename(rom)
        self.events.put_nowait(RomInfoEvent(filename=rom))

    async def _tick_loop(self) -> None:
        last_rom_check = time.monotonic()
        while self._running:
            try:
                self._practice_timing.tick()
                self._hyper_play_timing.tick()
                now = time.monotonic()
                if now - last_rom_check >= ROM_CHECK_INTERVAL_SEC:
                    last_rom_check = now
                    await self._check_rom_change()
            except Exception as exc:
                log.error(
                    logger, "RetroArchOrchestrator: tick error",
                    exc=exc,
                    practice_armed=self._practice_timing.is_armed,
                    hyper_play_armed=self._hyper_play_timing.is_armed,
                )
            await asyncio.sleep(TICK_INTERVAL_SEC)


