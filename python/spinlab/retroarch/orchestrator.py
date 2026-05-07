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
import logging
from collections.abc import Callable

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
from spinlab.retroarch.conditions import ConditionRegistry
from spinlab.retroarch.conditions_loader import apply_definitions
from spinlab.retroarch.event_adapter import to_protocol_dict, to_rom_info_dict
from spinlab.retroarch.events import TransitionEvent
from spinlab.retroarch.exceptions import NCIError
from spinlab.retroarch.timing import PracticeTiming, SpeedRunTiming

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
    ) -> None:
        self._client = client
        self._state_io = state_io
        self._poller = poller
        self._conditions = conditions
        self._practice_timing = practice_timing
        self._speed_run_timing = speed_run_timing

        # TcpManager public surface
        self.events: asyncio.Queue[dict] = asyncio.Queue()
        self.on_disconnect: Callable | None = None

        self._connected = False
        self._running = False
        self._poller_task: asyncio.Task | None = None
        self._tick_task: asyncio.Task | None = None

        # Build dispatch table once; handlers are bound methods.
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
            ReferenceStartCmd: self._unsupported_phase_e,
            ReferenceStopCmd: self._unsupported_phase_e,
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
            logger.warning("RetroArch NCI not reachable: %s", exc)
            return False

        # Emit rom_info to mimic Lua's startup behaviour so the dashboard
        # session sees the game name immediately.
        try:
            status = self._client.get_status()
            self.events.put_nowait(to_rom_info_dict(status))
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

    async def _on_fill_gap_load(self, cmd: FillGapLoadCmd) -> None:
        # No cold-fill activation — capture controller handles fill-gap spawn
        # observation independently.
        self._state_io.load_state_from_path(cmd.state_path)
        self._poller.mark_state_loaded()

    async def _on_reset(self, cmd: ResetCmd) -> None:
        self._client.reset()

    async def _on_set_conditions(self, cmd: SetConditionsCmd) -> None:
        apply_definitions(self._conditions, cmd.definitions)

    async def _on_set_invalidate_combo(self, cmd: SetInvalidateComboCmd) -> None:
        # Under the RetroArch backend the invalidate combo is handled via a
        # dashboard button; there is no emulator-side hotkey to wire up.
        logger.info(
            "RetroArchOrchestrator: invalidate combo is dashboard-button only under RA backend; ignoring %r",
            cmd.combo,
        )

    async def _on_game_context(self, cmd: GameContextCmd) -> None:
        # RA doesn't need an outbound game-context message — the ROM is already
        # loaded in RetroArch before the dashboard starts.
        logger.info(
            "RetroArchOrchestrator: GameContext is informational only; game already loaded in RA",
        )

    async def _unsupported_phase_e(self, cmd) -> None:
        raise NotImplementedError(
            f"BSV record/replay not wired in F-live; coming in Phase E. cmd={type(cmd).__name__}"
        )

    # ------------------------------------------------------------------
    # Event plumbing
    # ------------------------------------------------------------------

    def on_poller_event(self, ev: TransitionEvent) -> None:
        """Sync callback for the poller's on_event. Convert to protocol dict + enqueue.

        Public so the factory/builder that wires the poller (e.g. in
        dashboard or build_orchestrator) can attach this as
        ``poller.deps.on_event``.
        """
        try:
            d = to_protocol_dict(ev)
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
    missing = [
        name
        for name, val in (
            ("savestate_dir", emu.savestate_dir),
            ("spinlab_state_dir", emu.spinlab_state_dir),
            ("ra_game_basename", emu.ra_game_basename),
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
        ra_game_basename=emu.ra_game_basename,
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
    orch = RetroArchOrchestrator(
        client=client,
        state_io=state_io,
        poller=poller,
        conditions=conditions,
        practice_timing=practice_timing,
        speed_run_timing=speed_run_timing,
    )
    # Wire the poller's event callback to the orchestrator now that it exists.
    deps.on_event = orch.on_poller_event
    return orch
