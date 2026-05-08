"""RetroArchOrchestrator — duck-typed TcpManager replacement.

Owns NCIClient + Poller + StateIO + timing modules + ConditionRegistry. Translates
typed protocol commands into NCI calls + state_io operations. Publishes events
into an asyncio.Queue shaped for session_manager.route_event.

Implements TcpManager's public surface (is_connected, events, on_disconnect,
connect, disconnect, send_command, recv_event) so existing callers (dashboard,
session_manager, capture controllers) work unchanged.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from spinlab.protocol import (
    ColdFillLoadCmd,
    FillGapLoadCmd,
    GameContextCmd,
    PracticeLoadCmd,
    PracticeStopCmd,
    ReferenceStartCmd,
    ReferenceStopCmd,
    ReplayCmd,
    ReplayStopCmd,
    ResetCmd,
    SetConditionsCmd,
    SetInvalidateComboCmd,
    SpeedRunLoadCmd,
    SpeedRunStopCmd,
)
from spinlab.condition_registry import ConditionRegistry
from spinlab.retroarch.exceptions import NCIError
from spinlab.retroarch.responses import StatusInfo
from spinlab.retroarch.timing import PracticeTiming, SpeedRunTiming


def _rom_info_dict(status: StatusInfo) -> dict:
    """Build a `rom_info` JSON dict from NCI's GET_STATUS reply.

    Used at orchestrator startup to mimic the Lua-emitted RomInfoEvent
    so the dashboard sees the game name immediately. Inlined here from the
    deleted event_adapter module — only one caller.
    """
    return {"event": "rom_info", "filename": status.game or ""}

logger = logging.getLogger(__name__)

# 20 Hz tick — fast enough for auto_advance_delay_ms precision without
# burning unnecessary CPU. The poller already runs at ~60 Hz; this only
# drives timing deadlines, not frame-by-frame detection.
TICK_INTERVAL_SEC = 0.05


class RetroArchOrchestrator:
    """TcpManager-shaped façade over NCI + state_io + poller + timing modules.

    Constructor accepts pre-built component instances so callers (and tests)
    can substitute fakes. All components are duck-typed; no ABC is enforced.
    """

    def __init__(
        self,
        *,
        client,                       # NCIClient (or fake with same surface)
        state_io,                     # StateIO (or fake)
        poller,                       # Poller (or fake: run/stop/mark_state_loaded/activate_cold_fill)
        conditions: ConditionRegistry,
        practice_timing: PracticeTiming,
        speed_run_timing: SpeedRunTiming,
        movie_recorder=None,          # Optional[MovieRecorder] — None disables recording
    ) -> None:
        self._client = client
        self._state_io = state_io
        self._poller = poller
        self._conditions = conditions
        self._practice_timing = practice_timing
        self._speed_run_timing = speed_run_timing
        self._movie_recorder = movie_recorder

        # TcpManager public surface
        self.events: asyncio.Queue[dict] = asyncio.Queue()
        self.on_disconnect: Callable | None = None

        self._connected = False
        self._running = False
        self._poller_task: asyncio.Task | None = None
        self._tick_task: asyncio.Task | None = None

        # Suppress the "NCI not reachable" warning after the first one in a
        # disconnect streak. The dashboard's event_loop polls connect() every
        # 2s; without suppression, an idle dashboard with RA not yet launched
        # spams the log. Reset on successful connect.
        self._not_reachable_warning_logged = False

        # Build dispatch table once; handlers are bound methods.
        # ReferenceStart/Stop now succeed (state captures only — no BSV).
        # Replay/ReplayStop still raise: those genuinely require BSV (Phase E).
        self._dispatch: dict[type, Callable] = {
            PracticeLoadCmd: self._on_practice_load,
            PracticeStopCmd: self._on_practice_stop,
            SpeedRunLoadCmd: self._on_speed_run_load,
            SpeedRunStopCmd: self._on_speed_run_stop,
            ColdFillLoadCmd: self._on_cold_fill_load,
            FillGapLoadCmd: self._on_fill_gap_load,
            ResetCmd: self._on_reset,
            SetConditionsCmd: self._on_set_conditions,
            SetInvalidateComboCmd: self._on_set_invalidate_combo,
            GameContextCmd: self._on_game_context,
            ReferenceStartCmd: self._on_reference_start,
            ReferenceStopCmd: self._on_reference_stop,
            ReplayCmd: self._unsupported_phase_e,
            ReplayStopCmd: self._unsupported_phase_e,
        }

    # ------------------------------------------------------------------
    # TcpManager public surface
    # ------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self, timeout: float = 5.0) -> bool:
        """Probe NCI, emit startup rom_info, start poller + tick loops."""
        try:
            self._client.version()
        except NCIError as exc:
            # Only log once per disconnect streak — event_loop retries every
            # 2s, so without suppression an idle dashboard waiting for RA
            # spams the log. Cleared on successful connect below.
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
        # Connected — clear the suppression so the next disconnect streak logs.
        self._not_reachable_warning_logged = False

        # Emit rom_info to mimic Lua's startup behaviour so the dashboard
        # session sees the game name immediately. Also update StateIO's slot
        # basename to whatever RA reports — this fixes the silent save
        # failure that bites users whose config.ra_game_basename doesn't
        # exactly match the loaded ROM filename (e.g. config says
        # "Toothpaste World" but ROM is "Toothpaste.smc").
        try:
            status = self._client.get_status()
            self.events.put_nowait(_rom_info_dict(status))
            if status.game and hasattr(self._state_io, "update_game_basename"):
                self._state_io.update_game_basename(status.game)
        except NCIError:
            logger.debug("RetroArchOrchestrator: GET_STATUS failed at startup; skipping rom_info")

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
            self._client.close()
        except Exception:
            logger.debug("RetroArchOrchestrator: client.close() raised during disconnect", exc_info=True)

        logger.info("RetroArchOrchestrator disconnected")

    async def save_state(self, segment_id: str) -> None:
        """EmuBackend Protocol method.

        StateIO.save_segment_state is sync (mtime polls a slot file) so we
        run it on a worker thread to keep the asyncio loop responsive.
        Capture controllers call this from inside route_event handlers; we
        await the worker so the caller can react to failures (logging,
        skipping the recorder write).
        """
        await asyncio.to_thread(self._state_io.save_segment_state, segment_id)

    async def load_state(self, state_path: str) -> None:
        """EmuBackend Protocol method.

        StateIO.load_state_from_path is sync (file copy + LOAD_STATE_SLOT)
        and runs on a worker thread. Marking the poller after load suppresses
        phantom-edge events on the snapshot taken right after RA reloads.
        """
        await asyncio.to_thread(self._state_io.load_state_from_path, state_path)
        self._poller.mark_state_loaded()

    async def recv_event(self, timeout: float | None = None) -> dict | None:
        """Pull one event dict off the queue. Returns None on timeout."""
        try:
            if timeout is not None:
                return await asyncio.wait_for(self.events.get(), timeout=timeout)
            return await self.events.get()
        except asyncio.TimeoutError:
            return None

    async def send_command(self, cmd) -> None:
        """Dispatch a typed protocol command to the appropriate handler.

        Unknown command types are logged at WARNING and silently dropped;
        they do not raise so callers remain unaffected by future command
        additions.
        """
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
        self._state_io.load_state_from_path(cmd.state_path)
        self._poller.mark_state_loaded()
        self._practice_timing.arm(
            segment_id=cmd.id,
            end_type=cmd.end_type,
            death_penalty_ms=cmd.death_penalty_ms,
            auto_advance_delay_ms=cmd.auto_advance_delay_ms,
            on_attempt_result=self._enqueue_dict,
        )

    async def _on_practice_stop(self, cmd: PracticeStopCmd) -> None:
        self._practice_timing.disarm()

    async def _on_speed_run_load(self, cmd: SpeedRunLoadCmd) -> None:
        self._state_io.load_state_from_path(cmd.state_path)
        self._poller.mark_state_loaded()
        self._speed_run_timing.arm(
            segment_id=cmd.id,
            checkpoints=list(cmd.checkpoints),
            auto_advance_delay_ms=cmd.auto_advance_delay_ms,
            death_delay_ms=cmd.death_delay_ms,
            on_event=self._enqueue_dict,
        )

    async def _on_speed_run_stop(self, cmd: SpeedRunStopCmd) -> None:
        self._speed_run_timing.disarm()

    async def _on_cold_fill_load(self, cmd: ColdFillLoadCmd) -> None:
        self._state_io.load_state_from_path(cmd.state_path)
        self._poller.mark_state_loaded()
        self._poller.activate_cold_fill(cmd.segment_id)
        logger.info(
            "cold_fill_load: state loaded and detector activated for segment=%s",
            cmd.segment_id,
        )

    async def _on_fill_gap_load(self, cmd: FillGapLoadCmd) -> None:
        # No cold-fill activation — capture controller handles fill-gap spawn
        # observation independently.
        self._state_io.load_state_from_path(cmd.state_path)
        self._poller.mark_state_loaded()

    async def _on_reset(self, cmd: ResetCmd) -> None:
        self._client.reset()

    async def _on_set_conditions(self, cmd: SetConditionsCmd) -> None:
        self._conditions.replace_with_read_specs(cmd.definitions)

    async def _on_set_invalidate_combo(self, cmd: SetInvalidateComboCmd) -> None:
        # Under the RetroArch backend the invalidate combo is handled via a
        # dashboard button; there is no emulator-side hotkey to wire up.
        # Debug-only because SessionManager sends this on every game switch
        # and the message is unactionable.
        logger.debug(
            "RetroArchOrchestrator: invalidate combo is dashboard-button only under RA backend; ignoring %r",
            cmd.combo,
        )

    async def _on_game_context(self, cmd: GameContextCmd) -> None:
        # RA doesn't need an outbound game-context message — the ROM is already
        # loaded in RetroArch before the dashboard starts.
        logger.info(
            "RetroArchOrchestrator: GameContext is informational only; game already loaded in RA",
        )

    async def _on_reference_start(self, cmd: ReferenceStartCmd) -> None:
        """Trigger movie recording if a recorder is configured. Failures are
        non-fatal — reference runs are about state captures; movie capture is
        supplementary.
        """
        if self._movie_recorder is None:
            logger.info("Reference recording started (no movie recorder configured)")
            return
        movie_path = Path(cmd.path).with_suffix(".replay")
        try:
            await asyncio.to_thread(self._movie_recorder.start, movie_path)
            logger.info("Movie recording started: %s", movie_path)
        except Exception as exc:
            logger.warning("Movie recording failed to start: %s", exc)

    async def _on_reference_stop(self, cmd: ReferenceStopCmd) -> None:
        """Stop movie recording if active. Failures are non-fatal."""
        if self._movie_recorder is None or not self._movie_recorder.is_recording():
            logger.info("Reference recording stopped (no movie recorder active)")
            return
        try:
            path = await asyncio.to_thread(self._movie_recorder.stop)
            logger.info("Movie recording stopped: %s", path)
        except Exception as exc:
            logger.warning("Movie recording failed to stop: %s", exc)

    async def _unsupported_phase_e(self, cmd) -> None:
        # Surfaces as a clean HTTP 501 via the dashboard's ActionError handler.
        # Replay genuinely needs BSV (libretro deterministic movie format),
        # which is the next migration phase.
        from spinlab.errors import BackendNotImplementedError
        logger.warning(
            "RetroArchOrchestrator: %s rejected — BSV replay (Phase E) not wired",
            type(cmd).__name__,
        )
        raise BackendNotImplementedError()

    # ------------------------------------------------------------------
    # Event plumbing
    # ------------------------------------------------------------------

    def on_poller_event(self, ev: Any) -> None:
        """Sync callback for the poller's on_event. Convert to dict + enqueue.

        Public so the factory/builder that wires the poller (e.g. in
        ``build_orchestrator``) can attach this as ``poller.deps.on_event``.

        Save-on-event and practice reload-on-death used to live here; they
        moved out in the 2026-05-07 backend-layering refactor. Capture
        controllers and PracticeSession now own those concerns by calling
        ``EmuBackend.save_state`` / ``EmuBackend.load_state`` directly.
        """
        # The detector emits protocol dataclasses (LevelEntranceEvent etc.).
        # asdict already populates the discriminator `event` field from the
        # dataclass default — no manual translation needed.
        try:
            d = dataclasses.asdict(ev)
        except TypeError:
            logger.warning(
                "RetroArchOrchestrator: dropping unknown event type %r",
                type(ev).__name__,
            )
            return
        # Feed timing state machines so attempt_result / speed_run_* events fire.
        self._practice_timing.observe_event(d)
        self._speed_run_timing.observe_event(d)
        self.events.put_nowait(d)

    def _enqueue_dict(self, d: dict) -> None:
        """Direct enqueue helper used as callback by PracticeTiming / SpeedRunTiming."""
        self.events.put_nowait(d)

    # ------------------------------------------------------------------
    # Background tasks
    # ------------------------------------------------------------------

    async def _tick_loop(self) -> None:
        """Periodic tick driving timing module deadlines (auto_advance_delay_ms)."""
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

    Imports are deferred inside the function to avoid circular import issues
    (AppConfig lives in spinlab.config; importing it at module level could
    create cycles via dashboard.py → orchestrator.py → config.py → ...).
    """
    emu = config.emulator
    if emu.backend != "retroarch":
        raise ValueError(
            f"build_orchestrator requires backend='retroarch', got {emu.backend!r}"
        )
    # ra_game_basename is intentionally NOT in the required list: the
    # orchestrator overrides it from RA's GET_STATUS at connect() time.
    # Listing it as required led users to set a stale/wrong value that
    # then silently broke save/load via mtime-polling timeouts.
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

    from spinlab.retroarch.nci import NCIClient
    from spinlab.retroarch.poller import DEFAULT_PERIOD_SEC, Poller, PollerDeps
    from spinlab.retroarch.snapshot import read_snapshot
    from spinlab.retroarch.state_io import StateIO

    client = NCIClient(host=config.network.host, port=config.network.nci_port)
    state_io = StateIO(
        client=client,
        ra_savestate_dir=emu.savestate_dir,
        spinlab_state_dir=emu.spinlab_state_dir,
        ra_game_basename=emu.ra_game_basename or "",  # auto-set on connect()
    )
    conditions = ConditionRegistry()
    practice_timing = PracticeTiming()
    speed_run_timing = SpeedRunTiming()

    deps = PollerDeps(
        client=client,
        read_snapshot=read_snapshot,
        on_event=lambda ev: None,  # rebound below after orch is constructed
        state_path_for=state_io.resolve_event_path,
        conditions_registry=conditions,
    )
    poller = Poller(deps, period_sec=DEFAULT_PERIOD_SEC)

    from spinlab.retroarch.movie import MovieRecorder, discover_movie_dir

    # Resolve where RA writes movie files. Priority:
    #   1. emu.ra_movie_dir (explicit override)
    #   2. discover_movie_dir(client, emu.ra_core_subdir) if ra_core_subdir available
    #   3. None — disables the recorder
    movie_dir: Path | None
    if emu.ra_movie_dir is not None:
        movie_dir = emu.ra_movie_dir
    elif emu.ra_core_subdir:
        try:
            movie_dir = discover_movie_dir(client, emu.ra_core_subdir)
        except Exception as exc:
            logger.warning(
                "build_orchestrator: movie recorder disabled — could not discover movie_dir: %s",
                exc,
            )
            movie_dir = None
    else:
        logger.info(
            "build_orchestrator: movie recorder disabled — neither emu.ra_movie_dir nor emu.ra_core_subdir set"
        )
        movie_dir = None

    movie_recorder = MovieRecorder(client=client, movie_dir=movie_dir) if movie_dir is not None else None

    orch = RetroArchOrchestrator(
        client=client,
        state_io=state_io,
        poller=poller,
        conditions=conditions,
        practice_timing=practice_timing,
        speed_run_timing=speed_run_timing,
        movie_recorder=movie_recorder,
    )
    # Wire the poller's event callback to the orchestrator now that it exists.
    deps.on_event = orch.on_poller_event
    return orch
