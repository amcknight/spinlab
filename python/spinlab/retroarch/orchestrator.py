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
from collections.abc import Callable
from pathlib import Path

from spinlab.condition_registry import ConditionRegistry
from spinlab.protocol import (
    SPEED_UNCAPPED,
    ColdFillLoadCmd,
    FillGapLoadCmd,
    PracticeLoadCmd,
    PracticeStopCmd,
    ReferenceStartCmd,
    ReferenceStopCmd,
    ReplayCmd,
    ReplayErrorEvent,
    ReplayFinishedEvent,
    ReplayStartedEvent,
    ReplayStopCmd,
    ResetCmd,
    RomInfoEvent,
    SetConditionsCmd,
    SpeedRunLoadCmd,
    SpeedRunStopCmd,
)
from spinlab.retroarch.raclient import (
    MoviePlayback,
    MoviePlaybackError,
    MovieRecording,
    NotReachableError,
    RAClient,
    RAClientError,
)
from spinlab.retroarch.timing import PracticeTiming, SpeedRunTiming
from spinlab.state_paths import StatePathResolver

logger = logging.getLogger(__name__)

# 20 Hz tick — fast enough for auto_advance_delay_ms precision without
# burning unnecessary CPU. The poller already runs at ~60 Hz; this only
# drives timing deadlines, not frame-by-frame detection.
TICK_INTERVAL_SEC = 0.05


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
        speed_run_timing: SpeedRunTiming,
        state_paths: StatePathResolver,
        enable_movies: bool = True,
    ) -> None:
        self._raclient = raclient
        self._poller = poller
        self._conditions = conditions
        self._practice_timing = practice_timing
        self._speed_run_timing = speed_run_timing
        self._state_paths = state_paths
        self._enable_movies = enable_movies

        # EmuBackend public surface
        self.events: asyncio.Queue[object] = asyncio.Queue()
        self.on_disconnect: Callable | None = None

        self._connected = False
        self._running = False
        self._poller_task: asyncio.Task | None = None
        self._tick_task: asyncio.Task | None = None

        # Active recording/playback handles, set between start/stop.
        self._active_recording: MovieRecording | None = None
        self._active_playback: MoviePlayback | None = None

        # Tracks whether we toggled RA into fast-forward during the current
        # replay so the stop path can toggle it back. NCI's FAST_FORWARD is a
        # flip with no state query, so symmetric toggling is the only safe
        # way to drive it from code.
        self._fast_forwarding: bool = False

        # Suppress the "NCI not reachable" warning after the first one in a
        # disconnect streak. The dashboard's event_loop polls connect() every
        # 2s; without suppression, an idle dashboard with RA not yet launched
        # spams the log. Reset on successful connect.
        self._not_reachable_warning_logged = False

        self._dispatch: dict[type, Callable] = {
            PracticeLoadCmd: self._on_practice_load,
            PracticeStopCmd: self._on_practice_stop,
            SpeedRunLoadCmd: self._on_speed_run_load,
            SpeedRunStopCmd: self._on_speed_run_stop,
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

        self._not_reachable_warning_logged = False
        self.events.put_nowait(RomInfoEvent(filename=info.rom_filename))

        self._connected = True
        self._running = True
        self._poller_task = asyncio.create_task(self._poller.run())
        self._tick_task = asyncio.create_task(self._tick_loop())
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
        await self._raclient.load_state(Path(cmd.state_path))
        self._practice_timing.arm(
            segment_id=cmd.id,
            end_type=cmd.end_type,
            death_penalty_ms=cmd.death_penalty_ms,
            auto_advance_delay_ms=cmd.auto_advance_delay_ms,
            on_attempt_result=self._enqueue,
        )

    async def _on_practice_stop(self, cmd: PracticeStopCmd) -> None:
        self._practice_timing.disarm()

    async def _on_speed_run_load(self, cmd: SpeedRunLoadCmd) -> None:
        await self._raclient.load_state(Path(cmd.state_path))
        self._speed_run_timing.arm(
            segment_id=cmd.id,
            checkpoints=list(cmd.checkpoints),
            auto_advance_delay_ms=cmd.auto_advance_delay_ms,
            death_delay_ms=cmd.death_delay_ms,
            on_event=self._enqueue,
        )

    async def _on_speed_run_stop(self, cmd: SpeedRunStopCmd) -> None:
        self._speed_run_timing.disarm()

    async def _on_cold_fill_load(self, cmd: ColdFillLoadCmd) -> None:
        await self._raclient.load_state(Path(cmd.state_path))
        self._poller.activate_cold_fill(cmd.segment_id)
        logger.info(
            "cold_fill_load: state loaded and detector activated for segment=%s",
            cmd.segment_id,
        )

    async def _on_fill_gap_load(self, cmd: FillGapLoadCmd) -> None:
        await self._raclient.load_state(Path(cmd.state_path))

    async def _on_reset(self, cmd: ResetCmd) -> None:
        await self._raclient.reset()

    async def _on_set_conditions(self, cmd: SetConditionsCmd) -> None:
        self._conditions.replace_with_read_specs(cmd.definitions)

    async def _on_reference_start(self, cmd: ReferenceStartCmd) -> None:
        """Start movie recording for the reference run. Failures are
        non-fatal — reference runs are about state captures; movie capture
        is supplementary.
        """
        if not self._enable_movies:
            logger.info("Reference recording started (movies disabled)")
            return
        movie_path = Path(cmd.path)
        try:
            self._active_recording = await self._raclient.record_movie(movie_path)
            logger.info("Movie recording started: %s", movie_path)
        except RAClientError as exc:
            logger.warning("Movie recording failed to start: %s", exc)

    async def _on_reference_stop(self, cmd: ReferenceStopCmd) -> None:
        """Stop movie recording if active. Failures are non-fatal."""
        if self._active_recording is None:
            logger.info("Reference recording stopped (no movie recorder active)")
            return
        try:
            path = await self._active_recording.stop()
            logger.info("Movie recording stopped: %s", path)
        except RAClientError as exc:
            logger.warning("Movie recording failed to stop: %s", exc)
        finally:
            self._active_recording = None

    async def _on_replay(self, cmd: ReplayCmd) -> None:
        """Start movie playback. cmd.path is the .replay path resolved by
        the dashboard.
        """
        if not self._enable_movies:
            from spinlab.errors import BackendNotImplementedError
            logger.warning("RetroArchOrchestrator: ReplayCmd rejected — movies disabled")
            raise BackendNotImplementedError()

        movie_path = Path(cmd.path)
        try:
            self._active_playback = await self._raclient.play_movie(movie_path)
        except MoviePlaybackError as exc:
            logger.error("Movie replay verification failed: %s", exc)
            self.on_poller_event(ReplayErrorEvent(message=str(exc)))
            return
        except RAClientError as exc:
            logger.error("Movie replay failed: %s", exc)
            self.on_poller_event(ReplayErrorEvent(message=str(exc)))
            return

        # Honor cmd.speed=SPEED_UNCAPPED by toggling RA into fast-forward.
        # RA's default is 60fps real-time playback; uncapped lets the host
        # CPU run the core as fast as it can — replay-fixture test drops
        # from ~47s (2273 frames @ 60fps) to a few seconds.
        if cmd.speed == SPEED_UNCAPPED:
            await asyncio.to_thread(self._raclient.fast_forward_toggle)
            self._fast_forwarding = True

        self.on_poller_event(ReplayStartedEvent(
            path=str(movie_path),
            frame_count=self._active_playback.frame_count,
        ))
        logger.info(
            "Movie replay started: %s (frames=%d, fast_forward=%s)",
            movie_path, self._active_playback.frame_count, self._fast_forwarding,
        )

    async def _on_replay_stop(self, cmd: ReplayStopCmd) -> None:
        """Stop movie playback and emit ReplayFinishedEvent. Idempotent."""
        if self._active_playback is None:
            return
        try:
            await self._active_playback.stop()
        except RAClientError as exc:
            logger.warning("Movie replay failed to stop: %s", exc)
        finally:
            self._active_playback = None
            if self._fast_forwarding:
                # Symmetric toggle: every FAST_FORWARD flips state.
                await asyncio.to_thread(self._raclient.fast_forward_toggle)
                self._fast_forwarding = False
        self.on_poller_event(ReplayFinishedEvent())
        logger.info("Movie replay stopped")

    # ------------------------------------------------------------------
    # Event plumbing
    # ------------------------------------------------------------------

    def on_poller_event(self, ev: object) -> None:
        """Sync callback for the poller's on_event. Feed timing modules + enqueue."""
        if self._practice_timing is not None:
            self._practice_timing.observe_event(ev)
        if self._speed_run_timing is not None:
            self._speed_run_timing.observe_event(ev)
        self.events.put_nowait(ev)

    def _enqueue(self, ev: object) -> None:
        self.events.put_nowait(ev)

    # ------------------------------------------------------------------
    # Background tasks
    # ------------------------------------------------------------------

    async def _tick_loop(self) -> None:
        while self._running:
            try:
                self._practice_timing.tick()
                self._speed_run_timing.tick()
            except Exception:
                logger.exception("RetroArchOrchestrator: tick error")
            await asyncio.sleep(TICK_INTERVAL_SEC)


def build_orchestrator(config) -> "RetroArchOrchestrator":
    """Construct a fully wired RetroArchOrchestrator from AppConfig.

    Raises ValueError if config.emulator is missing required RA fields.
    """
    emu = config.emulator
    missing = [
        name
        for name, val in (
            ("savestate_dir", emu.savestate_dir),
            ("spinlab_state_dir", emu.spinlab_state_dir),
        )
        if val is None
    ]
    if missing:
        raise ValueError(
            f"build_orchestrator: emulator.{', emulator.'.join(missing)} required for retroarch backend"
        )
    # Type-narrow for the rest of the function — checked above.
    savestate_dir: Path = emu.savestate_dir
    spinlab_state_dir: Path = emu.spinlab_state_dir

    from spinlab.retroarch.poller import DEFAULT_PERIOD_SEC, Poller, PollerDeps
    from spinlab.retroarch.snapshot import read_snapshot

    # Resolve where RA writes movie files. Priority:
    #   1. emu.ra_movie_dir (explicit override)
    #   2. emu.savestate_dir / emu.ra_core_subdir (derived from existing config)
    #   3. None — disables recorder/player
    movie_dir: Path | None
    if emu.ra_movie_dir is not None:
        movie_dir = emu.ra_movie_dir
    elif emu.ra_core_subdir:
        # Don't double-append: users sometimes set savestate_dir to the
        # per-core subdir directly (e.g. C:\RetroArch-Win64\states\Snes9x).
        if savestate_dir.name == emu.ra_core_subdir:
            movie_dir = savestate_dir
        else:
            movie_dir = savestate_dir / emu.ra_core_subdir
        logger.info("build_orchestrator: movie_dir derived as %s", movie_dir)
    else:
        logger.info(
            "build_orchestrator: movie recorder/player disabled — set emu.ra_movie_dir "
            "or emu.savestate_dir + emu.ra_core_subdir to enable",
        )
        movie_dir = None

    # RA's logs dir, derived from retroarch_path. Enables replay-slot
    # resolution; falls back to slot 0 if unavailable.
    ra_log_dir: Path | None = None
    if emu.retroarch_path is not None:
        candidate = emu.retroarch_path.parent / "logs"
        if candidate.exists():
            ra_log_dir = candidate
        else:
            logger.info(
                "build_orchestrator: RA logs dir not found at %s — replay "
                "slot lookup will fall back to slot 0. Enable log_to_file "
                'in retroarch.cfg to fix.', candidate,
            )

    raclient = RAClient(
        host=config.network.host,
        port=config.network.nci_port,
        ra_savestate_dir=savestate_dir,
        ra_log_dir=ra_log_dir,
        ra_movie_dir=movie_dir,
    )

    conditions = ConditionRegistry()
    practice_timing = PracticeTiming()
    speed_run_timing = SpeedRunTiming()
    state_paths = StatePathResolver(spinlab_state_dir)

    deps = PollerDeps(
        client=raclient.nci,
        read_snapshot=read_snapshot,
        on_event=lambda ev: None,  # rebound below
        state_path_for=state_paths.resolve_event,
        conditions_registry=conditions,
        state_version=lambda: raclient.state_version,
    )
    poller = Poller(deps, period_sec=DEFAULT_PERIOD_SEC)

    orch = RetroArchOrchestrator(
        raclient=raclient,
        poller=poller,
        conditions=conditions,
        practice_timing=practice_timing,
        speed_run_timing=speed_run_timing,
        state_paths=state_paths,
        enable_movies=movie_dir is not None,
    )
    deps.on_event = orch.on_poller_event
    return orch
