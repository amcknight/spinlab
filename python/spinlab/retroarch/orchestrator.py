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

        # When True, save_eligible transition events trigger StateIO saves.
        # Set by ReferenceStart, cleared by ReferenceStop. State files are
        # *separate* from BSV input recording — practice can use the captured
        # states without ever recording inputs.
        self._recording = False

        # Suppress the "NCI not reachable" warning after the first one in a
        # disconnect streak. The dashboard's event_loop polls connect() every
        # 2s; without suppression, an idle dashboard with RA not yet launched
        # spams the log. Reset on successful connect.
        self._not_reachable_warning_logged = False

        # State path of the currently-armed practice segment, for reload-on-
        # death. Lua's practice loop did `table.insert(pending_loads, ...)`
        # whenever the player died; we replicate by remembering the path
        # from PracticeLoadCmd and re-loading it on Death events.
        self._practice_state_path: str | None = None

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
            self.events.put_nowait(to_rom_info_dict(status))
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
        self._practice_state_path = cmd.state_path
        self._practice_timing.arm(
            segment_id=cmd.id,
            end_type=cmd.end_type,
            death_penalty_ms=cmd.death_penalty_ms,
            auto_advance_delay_ms=cmd.auto_advance_delay_ms,
            on_attempt_result=self._on_practice_attempt_result,
        )

    async def _on_practice_stop(self, cmd: PracticeStopCmd) -> None:
        self._practice_timing.disarm()
        self._practice_state_path = None

    def _on_practice_attempt_result(self, result: dict) -> None:
        """Practice attempt completed — clear state path and forward to session."""
        self._practice_state_path = None
        self._enqueue_dict(result)

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

    async def _on_reference_start(self, cmd: ReferenceStartCmd) -> None:
        """Begin a reference run: enable state-capture-on-event.

        Under RA backend we don't write a .spinrec input recording (that's BSV
        — Phase E). But state captures and segment DB rows are independent of
        input recording: they're driven by transition events. Setting
        `_recording = True` causes save-eligible events (LevelEntrance,
        Checkpoint) to trigger `state_io.save_segment_state` so the path
        stamped on the event by `state_io.resolve_event_path` actually exists
        on disk by the time the recorder consumes it.
        """
        self._recording = True
        logger.info(
            "Reference recording started under retroarch backend "
            "(state captures only — no input recording until Phase E)"
        )

    async def _on_reference_stop(self, cmd: ReferenceStopCmd) -> None:
        """End reference recording. Emit a synthetic rec_saved so session_manager
        can finalize the run with an empty replay path."""
        self._recording = False
        # session_manager.handle_rec_saved just stores the path on the recorder.
        # An empty path under RA correctly signals 'no replay file'; the
        # captured states + segment rows are what the practice loop needs.
        self._enqueue_dict({"event": "rec_saved", "path": "", "frame_count": 0})
        logger.info("Reference recording stopped (no .spinrec under RA backend)")

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

    def on_poller_event(self, ev: TransitionEvent) -> None:
        """Sync callback for the poller's on_event. Convert to protocol dict + enqueue.

        Public so the factory/builder that wires the poller (e.g. in
        dashboard or build_orchestrator) can attach this as
        ``poller.deps.on_event``.
        """
        # Persist a state file BEFORE handing off the event, so the path
        # stamped on it by state_io.resolve_event_path actually exists by the
        # time the recorder consumes it. (Lua wrote the file before emitting;
        # under NCI, the orchestrator does.) Best-effort — failures log and
        # the event still flows; the consumer's handling of a missing file is
        # already in place (recorder skips entries with no state_path).
        self._maybe_save_state_for(ev)

        # Practice mode: reload the segment's start state on Death so the
        # player retries from the segment boundary, not from wherever they
        # respawned. Lua did `table.insert(pending_loads, segment.state_path)`;
        # we run it in a worker thread so the asyncio loop stays responsive.
        self._maybe_reload_state_on_death(ev)

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

    def _maybe_save_state_for(self, ev: TransitionEvent) -> None:
        """Capture a savestate if this event needs to back its `state_path` field.

        Two cases trigger a save:
          - Cold-fill spawn (`is_cold_cp=True` with a segment_id): always save,
            since the cold path is being captured into a segment regardless of
            reference-recording mode.
          - LevelEntrance / Checkpoint during reference recording: save so the
            path the resolver stamped on the event resolves to a real file.

        Practice and speed-run modes deliberately don't save here — they're
        consumers, not producers. Saving in those modes would clobber the
        reference's captured states.
        """
        from spinlab.retroarch.events import Checkpoint, LevelEntrance, Spawn

        seg_id: str | None = None
        if isinstance(ev, Spawn) and ev.is_cold_cp and ev.segment_id:
            seg_id = ev.segment_id
        elif self._recording:
            if isinstance(ev, LevelEntrance):
                seg_id = f"entrance_{ev.level}_{ev.room}"
            elif isinstance(ev, Checkpoint):
                seg_id = f"cp_{ev.level_num}_{ev.cp_ordinal}_hot"
        if seg_id is None:
            return

        # Run save in a worker thread so a slow SAVE_STATE (mtime polling can
        # block up to save_timeout_sec) doesn't freeze the asyncio event loop.
        # Without this, a single misbehaving save stalls the whole dashboard
        # for the entire timeout window. The save still completes well before
        # the user clicks Save & Finish — the recorder only stores state_path
        # in the DB at handle-entrance time, it doesn't read the file then.
        try:
            asyncio.create_task(self._save_state_async(seg_id, type(ev).__name__))
        except RuntimeError:
            # No running loop (e.g., poller called us off-loop in a test). Fall
            # back to synchronous save so unit tests still see the call.
            self._save_state_sync(seg_id, type(ev).__name__)
        return

    async def _save_state_async(self, seg_id: str, ev_name: str) -> None:
        try:
            await asyncio.to_thread(self._state_io.save_segment_state, seg_id)
        except Exception:
            logger.exception(
                "save_segment_state failed for %r (segment_id=%r); event "
                "flowed with stale state_path",
                ev_name, seg_id,
            )

    def _maybe_reload_state_on_death(self, ev: TransitionEvent) -> None:
        """Practice-mode death → reload the segment's start state.

        Lua's `handle_practice` did this synchronously on every detected
        death frame. The state path is the one PracticeLoadCmd handed us;
        we reload via state_io.load_state_from_path on a worker thread so
        SAVE/LOAD's mtime polling doesn't block the asyncio loop.

        Also fires on pit-fall / death-fall LevelExits (goal=='abort'):
        those are deaths from the player's perspective even though the
        narrow `is_death_frame` (anim=9) doesn't catch them. Without this
        the player has to wait for AttemptResult's auto_advance_delay_ms
        before the reload kicks in via the next PracticeLoadCmd.
        """
        from spinlab.retroarch.events import Death, LevelExit

        is_death = isinstance(ev, Death)
        is_death_fall = isinstance(ev, LevelExit) and ev.goal == "abort"
        if not (is_death or is_death_fall):
            return
        if not self._practice_timing.is_armed:
            logger.info(
                "practice reload-on-death skipped: timing not armed (ev=%s)",
                type(ev).__name__,
            )
            return
        path = self._practice_state_path
        if not path:
            logger.warning(
                "practice death observed but no state_path remembered — skipping reload"
            )
            return
        logger.info(
            "practice reload-on-death triggered (ev=%s, path=%s)",
            type(ev).__name__, path,
        )
        try:
            asyncio.create_task(self._reload_state_async(path))
        except RuntimeError:
            # No running loop (test contexts) — fall back to sync.
            try:
                self._state_io.load_state_from_path(path)
                self._poller.mark_state_loaded()
            except Exception:
                logger.exception("practice reload-on-death (sync) failed")

    async def _reload_state_async(self, path: str) -> None:
        try:
            await asyncio.to_thread(self._state_io.load_state_from_path, path)
            self._poller.mark_state_loaded()
            logger.info("practice reload-on-death: state loaded (path=%s)", path)
        except Exception:
            logger.exception("practice reload-on-death failed (path=%s)", path)

    def _save_state_sync(self, seg_id: str, ev_name: str) -> None:
        try:
            self._state_io.save_segment_state(seg_id)
        except Exception:
            logger.exception(
                "save_segment_state failed for %r (segment_id=%r); event "
                "will flow with stale state_path",
                ev_name, seg_id,
            )

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
