"""RetroArchOrchestrator — implements the EmuBackend protocol.

Owns NCIClient + Poller + StateIO + timing modules + ConditionRegistry.
Translates typed protocol commands into NCI calls + state_io operations.
Publishes typed protocol events into an asyncio.Queue consumed by
session_manager.route_event.
"""
from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from pathlib import Path

from spinlab.condition_registry import ConditionRegistry
from spinlab.protocol import (
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
from spinlab.retroarch.exceptions import NCIError
from spinlab.retroarch.timing import PracticeTiming, SpeedRunTiming

logger = logging.getLogger(__name__)

# Pattern for parsing RA's log lines that report the current replay slot.
# RA emits in three forms:
#   - At startup: "[Replay] Found last replay slot: #N"
#   - After SLOT_PLUS/MINUS: "[Replay] Replay slot: N"
#   - When recording starts: "[Replay] Starting movie record to "<path>.replay<N>"."
#     This last one is critical: with replay_auto_index="true", RA bumps the
#     runtime slot to N when starting a recording, but does NOT emit a
#     "Replay slot:" line. Without this third match, the parser misses the
#     auto-index bump and stages at the stale pre-record slot.
# The latest match in the log is RA's current runtime slot.
_REPLAY_SLOT_LOG_PATTERN = re.compile(
    r'\[Replay\] (?:'
    r"Replay slot: |"
    r"Found last replay slot: #|"
    r'Starting movie record to "[^"]*\.replay'
    r")(\d+)"
)

# 20 Hz tick — fast enough for auto_advance_delay_ms precision without
# burning unnecessary CPU. The poller already runs at ~60 Hz; this only
# drives timing deadlines, not frame-by-frame detection.
TICK_INTERVAL_SEC = 0.05

# Spacing between the two NCI RESET fires that satisfy RA's two-press
# anti-accident confirmation. RA's input layer debounces hotkeys at ~6Hz
# (one tap per ~167ms) but also requires the second press to fall inside
# its confirmation window; 300ms is comfortably inside both bounds.
_RESET_DOUBLE_TAP_GAP_SEC = 0.3


class RetroArchOrchestrator:
    """EmuBackend implementation over NCI + state_io + poller + timing modules.

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
        movie_player=None,            # Optional[MoviePlayer] — None disables replay
        ra_log_dir: Path | None = None,  # RA's logs/ dir; used to find runtime replay slot
    ) -> None:
        self._client = client
        self._state_io = state_io
        self._poller = poller
        self._conditions = conditions
        self._practice_timing = practice_timing
        self._speed_run_timing = speed_run_timing
        self._movie_recorder = movie_recorder
        self._movie_player = movie_player
        self._ra_log_dir = ra_log_dir

        # EmuBackend public surface
        self.events: asyncio.Queue[object] = asyncio.Queue()
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

        # Emit rom_info at startup so the dashboard sees the game name
        # immediately. Also update StateIO's slot basename to whatever RA
        # reports — this fixes the silent save failure that bites users
        # whose config.ra_game_basename doesn't exactly match the loaded
        # ROM filename (e.g. config says "Toothpaste World" but ROM is
        # "Toothpaste.smc").
        try:
            status = self._client.get_status()
            self.events.put_nowait(RomInfoEvent(filename=status.game or ""))
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

    async def recv_event(self, timeout: float | None = None) -> object | None:
        """Pull one typed event off the queue. Returns None on timeout."""
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
            on_attempt_result=self._enqueue,
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
            on_event=self._enqueue,
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
        # RA's RESET hotkey requires two presses in a row before the console
        # actually resets — intentional anti-accident gate so people chatting
        # with their controller in hand don't reset their own runs. A single
        # NCI RESET counts as the first press; the second press has to come
        # within RA's confirmation window. Without firing twice, callers
        # (notably cold-fill completion) saw "tried to reset but stayed in
        # the level" symptoms and had to finish the reset manually.
        self._client.reset()
        await asyncio.sleep(_RESET_DOUBLE_TAP_GAP_SEC)
        self._client.reset()

    async def _on_set_conditions(self, cmd: SetConditionsCmd) -> None:
        self._conditions.replace_with_read_specs(cmd.definitions)

    async def _on_reference_start(self, cmd: ReferenceStartCmd) -> None:
        """Trigger movie recording if a recorder is configured. Failures are
        non-fatal — reference runs are about state captures; movie capture is
        supplementary.
        """
        if self._movie_recorder is None:
            logger.info("Reference recording started (no movie recorder configured)")
            return
        movie_path = Path(cmd.path)  # already a .replay path post-Phase-G
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

    async def _on_replay(self, cmd: ReplayCmd) -> None:
        """Start movie playback. cmd.path is the .replay path the dashboard
        resolved from the ref_id.

        Stages the source as `<game_basename>.replay0` so RA's PLAY_REPLAY
        finds it (RA looks for `<game>.replay<replay_slot>` where slot
        defaults to 0; our replay_max_keep=99 cfg keeps it from clobbering
        user data).

        Verifies playback actually started by sampling memory before/after
        a brief sleep — if RA refused the file (e.g. ROM-checksum mismatch),
        the popup error is invisible to NCI but memory will not advance.
        On verification failure: emits ReplayErrorEvent + halts.

        Emits ReplayStartedEvent synthetically on success. frame_count
        comes from a sibling .json metadata file when present, else 0.
        """
        if self._movie_player is None:
            from spinlab.errors import BackendNotImplementedError
            logger.warning("RetroArchOrchestrator: ReplayCmd rejected — no MoviePlayer configured")
            raise BackendNotImplementedError()
        movie_path = Path(cmd.path)  # already a .replay path post-Phase-G
        # game_basename is auto-set on connect() from RA's GET_STATUS reply;
        # if for some reason it's missing, we fall back to the generic stage
        # name (RA will likely fail-to-load and the verification below catches it).
        basename = getattr(self._state_io, "game_basename", None) or None

        # Find what slot RA's currently at by reading RA's log. RA's
        # `replay_auto_index = "true"` persists the runtime slot per-game
        # across sessions and there's no NCI command to query/set it
        # (SLOT_MINUS works but RA debounces it to ~6Hz, making slot
        # navigation prohibitively slow). Reading the log is the cleanest
        # signal — RA always logs "Found last replay slot: #N" at startup
        # and "Replay slot: N" after each SLOT change. Default to 0 if
        # we can't read the log.
        # See docs/retroarch-migration/slot-management.md for the full story.
        target_slot = self._find_current_replay_slot() or 0
        logger.info(
            "Movie replay: staging at slot %d (per RA log)", target_slot
        )

        await asyncio.to_thread(
            self._movie_player.play, movie_path,
            staged_basename=basename, staged_slot=target_slot,
        )

        # Verify RA actually started playing the movie. RA's "Failed to load
        # movie file" popup is in-app only — invisible over NCI. If the file
        # didn't load, the core stays where it was and memory won't advance.
        # Sample any WRAM byte before/after a small sleep to confirm progress.
        if not await self._verify_playback_advanced():
            await asyncio.to_thread(self._movie_player.stop)
            msg = (
                f"RA refused to load movie file {movie_path.name} (likely "
                "ROM-checksum mismatch or unreadable file). Check RA's log "
                "(retroarch.cfg log_to_file=\"true\") for the underlying error."
            )
            logger.error("Movie replay verification failed: %s", msg)
            self.on_poller_event(ReplayErrorEvent(message=msg))
            return

        # Resolve frame count from sibling metadata if present.
        frame_count = 0
        meta_path = movie_path.with_suffix(".json")
        if meta_path.exists():
            try:
                import json as _json
                frame_count = int(_json.loads(meta_path.read_text()).get("frame_count", 0))
            except Exception as exc:
                logger.warning("Could not read frame_count from %s: %s", meta_path, exc)

        self.on_poller_event(ReplayStartedEvent(path=str(movie_path), frame_count=frame_count))
        logger.info("Movie replay started: %s (frames=%d)", movie_path, frame_count)

    def _find_current_replay_slot(self) -> int | None:
        """Read RA's most recent log file to find the current runtime
        replay slot. RA logs "Found last replay slot: #N" at startup and
        "Replay slot: N" after each slot navigation. The latest match in
        the log is RA's current slot. Returns None if log unavailable
        (file not enabled, dir missing, no replay log lines yet).
        """
        if self._ra_log_dir is None or not self._ra_log_dir.exists():
            return None
        log_files = sorted(
            self._ra_log_dir.glob("retroarch__*.log"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not log_files:
            return None
        latest = log_files[0]
        try:
            text = latest.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("Could not read RA log %s: %s", latest, exc)
            return None
        matches = _REPLAY_SLOT_LOG_PATTERN.findall(text)
        if not matches:
            return None
        return int(matches[-1])

    async def _verify_playback_advanced(self) -> bool:
        """Returns True if WRAM advances over a brief sample window — i.e.
        RA is actively running frames. Used after PLAY_REPLAY to detect
        silent load failures.
        """
        try:
            before = await asyncio.to_thread(self._client.read_ram, 0x0000, 16)
            await asyncio.sleep(0.15)  # >= 9 frames at 60Hz; reliably caught
            after = await asyncio.to_thread(self._client.read_ram, 0x0000, 16)
        except Exception as exc:
            logger.warning("Movie replay verification: read_ram failed: %s", exc)
            return False
        return before != after

    async def _on_replay_stop(self, cmd: ReplayStopCmd) -> None:
        """Stop movie playback and emit ReplayFinishedEvent. Idempotent."""
        if self._movie_player is None or not self._movie_player.is_playing():
            return
        await asyncio.to_thread(self._movie_player.stop)
        self.on_poller_event(ReplayFinishedEvent())
        logger.info("Movie replay stopped")

    # ------------------------------------------------------------------
    # Event plumbing
    # ------------------------------------------------------------------

    def on_poller_event(self, ev: object) -> None:
        """Sync callback for the poller's on_event. Feed timing + enqueue.

        Public so the factory/builder that wires the poller (e.g. in
        ``build_orchestrator``) can attach this as ``poller.deps.on_event``.

        The poller emits typed protocol events (LevelEntranceEvent etc.).
        """
        # Guard against None for tests that construct with timing=None.
        if self._practice_timing is not None:
            self._practice_timing.observe_event(ev)
        if self._speed_run_timing is not None:
            self._speed_run_timing.observe_event(ev)
        self.events.put_nowait(ev)

    def _enqueue(self, ev: object) -> None:
        """Direct enqueue helper used as callback by PracticeTiming / SpeedRunTiming."""
        self.events.put_nowait(ev)

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

    from spinlab.retroarch.movie import MoviePlayer, MovieRecorder

    # Resolve where RA writes movie files. Priority:
    #   1. emu.ra_movie_dir (explicit override)
    #   2. emu.savestate_dir / emu.ra_core_subdir (derived from existing config)
    #   3. None — disables recorder/player
    # No NCI call needed — both fields are already in config, so this works even
    # when RA is not yet running (the common case at dashboard startup).
    movie_dir: Path | None
    if emu.ra_movie_dir is not None:
        movie_dir = emu.ra_movie_dir
    elif emu.savestate_dir is not None and emu.ra_core_subdir:
        # Don't double-append if savestate_dir already ends with ra_core_subdir.
        # Both conventions are seen in the wild: RA's actual savestate_directory
        # is typically the parent dir (e.g. C:\RetroArch-Win64\states), but
        # users sometimes set savestate_dir to the per-core subdir directly
        # (e.g. C:\RetroArch-Win64\states\Snes9x) since that's where their
        # .state files actually live for snes9x.
        if emu.savestate_dir.name == emu.ra_core_subdir:
            movie_dir = emu.savestate_dir
        else:
            movie_dir = emu.savestate_dir / emu.ra_core_subdir
        logger.info("build_orchestrator: movie_dir derived as %s", movie_dir)
    else:
        logger.info(
            "build_orchestrator: movie recorder/player disabled — set emu.ra_movie_dir "
            "or emu.savestate_dir + emu.ra_core_subdir to enable",
        )
        movie_dir = None

    movie_recorder = MovieRecorder(client=client, movie_dir=movie_dir) if movie_dir is not None else None
    movie_player = MoviePlayer(client=client, movie_dir=movie_dir) if movie_dir is not None else None

    # RA's logs/ dir, derived from retroarch_path. Used by _on_replay to
    # find RA's runtime replay slot (see _find_current_replay_slot).
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

    orch = RetroArchOrchestrator(
        client=client,
        state_io=state_io,
        poller=poller,
        conditions=conditions,
        practice_timing=practice_timing,
        speed_run_timing=speed_run_timing,
        movie_recorder=movie_recorder,
        movie_player=movie_player,
        ra_log_dir=ra_log_dir,
    )
    # Wire the poller's event callback to the orchestrator now that it exists.
    deps.on_event = orch.on_poller_event
    return orch
