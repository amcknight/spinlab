"""Practice session loop — runs as async background task in dashboard."""
from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Callable

from spinlab import log

from .allocators import SegmentWithModel
from .models import (
    Attempt,
    AttemptOutcome,
    AttemptSource,
    EventAttempt,
    SegmentCommand,
)
from .protocol import (
    AttemptResultEvent,
    EventAttemptEmission,
    PracticeLoadCmd,
    PracticePauseCmd,
    PracticeStopCmd,
)
from .scheduler import Scheduler

if TYPE_CHECKING:
    from .db import Database
    from .emu_backend import EmuBackend

logger = logging.getLogger(__name__)

SEGMENT_LOAD_TIMEOUT_S = 1.0


class PracticeSession:
    """Manages a practice session: picks segments, loads them, processes results."""

    def __init__(
        self,
        emu: "EmuBackend",
        db: "Database",
        game_id: str,
        *,
        scheduler: Scheduler,
        auto_advance_delay_ms: int = 1000,
        death_penalty_ms: int = 3200,
        on_attempt: Callable | None = None,
        on_segment_load: Callable | None = None,
        session_id: str | None = None,
    ) -> None:
        self.emu = emu
        self.db = db
        self.game_id = game_id
        self.auto_advance_delay_ms = auto_advance_delay_ms
        self.death_penalty_ms = death_penalty_ms
        self.on_attempt = on_attempt
        # Fired in run_one once a segment is picked and its load cmd is sent,
        # so a fresh state broadcast carries current_segment and the live
        # practice card renders immediately (not just after the first result).
        self.on_segment_load = on_segment_load

        self.scheduler = scheduler
        self.session_id = session_id or uuid.uuid4().hex
        self.started_at = datetime.now(UTC).isoformat()
        # Attempts FK to sessions(id); the row must exist before any attempt
        # is logged. Idempotent in tests that pre-seed the row with the same id.
        self.db.create_session(self.session_id, self.game_id)

        self.is_running = False
        self.current_segment_id: str | None = None
        self.segments_attempted = 0
        self.segments_completed = 0

        self.initial_expected_total_ms: float | None = None
        self.initial_expected_clean_ms: float | None = None

        self._result_event = asyncio.Event()
        self._result_data: AttemptResultEvent | None = None
        self._last_allocator: str | None = None

        # Track which episode_id we've persisted events for in this attempt.
        # Set by receive_event_attempt; cleared at the start of each run_one
        # cycle and read by _process_result to decide whether the events
        # path already wrote the row (production) or we need the shim
        # fallback (tests that deliver AttemptResultEvent without prior
        # EventAttemptEmissions).
        self._current_episode_id: str | None = None

        # Segment IDs whose state_path has been observed missing during this
        # session. _snapshot_expected_times silently skips such segments, which
        # makes the dashboard's "saved time" panel shrink without explanation;
        # logging once per segment surfaces the cause without spamming.
        self._missing_state_paths: set[str] = set()

        # Doubles as the "armed for reload-on-death" flag: non-None means
        # an attempt is in flight and Death / LevelExit(abort) should
        # trigger a backend.load_state. Cleared the moment attempt_result
        # arrives in receive_result (NOT at run_one cleanup) so a Death
        # arriving between result and tear-down doesn't cause a spurious
        # post-attempt reload.
        self._current_state_path: str | None = None

        # --- Practice Pause (R+X) state -------------------------------------
        # paused: True while the session clock is frozen and the in-flight
        # attempt has been dropped (backend timing disarmed). paused_at_epoch
        # marks the current pause's start (wall-clock epoch seconds);
        # pause_offset_sec accumulates completed paused spans. The route bar
        # subtracts both to freeze elapsed + savings/hr. _current_load_cmd is
        # the segment's PracticeLoadCmd, re-sent on resume to reload + re-arm.
        self.paused: bool = False
        self.paused_at_epoch: float | None = None
        self.pause_offset_sec: float = 0.0
        self._current_load_cmd: PracticeLoadCmd | None = None

    def _snapshot_expected_times(
        self, estimator_name: str
    ) -> tuple[float | None, float | None]:
        """Sum expected_ms across practicable segments using the named estimator.

        A segment contributes iff it has a state_path that exists on disk AND
        the estimator produced a non-None expected_ms. Missing clean estimates
        contribute 0 to clean; missing total estimates contribute 0 to total.
        Returns (None, None) if every segment lacked both estimates.
        """
        segments = SegmentWithModel.load_all(self.db, self.game_id, estimator_name)
        total_sum = 0.0
        clean_sum = 0.0
        any_total = False
        any_clean = False
        for seg in segments:
            if not seg.state_path or not os.path.exists(seg.state_path):
                if seg.segment_id not in self._missing_state_paths:
                    log.warn(
                        logger,
                        "practice: segment state file missing — excluded from expected-time totals",
                        segment_id=seg.segment_id,
                        state_path=seg.state_path or "",
                    )
                    self._missing_state_paths.add(seg.segment_id)
                continue
            output = seg.model_outputs.get(estimator_name)
            if output is None:
                continue
            if output.total.expected_ms is not None:
                total_sum += output.total.expected_ms
                any_total = True
            if output.clean.expected_ms is not None:
                clean_sum += output.clean.expected_ms
                any_clean = True
        return (
            total_sum if any_total else None,
            clean_sum if any_clean else None,
        )

    def current_expected_times(self) -> tuple[float | None, float | None]:
        """Current sum of expected_ms across practicable segments, using the
        scheduler's currently selected estimator."""
        return self._snapshot_expected_times(self.scheduler.estimator.name)

    def start(self) -> None:
        (
            self.initial_expected_total_ms,
            self.initial_expected_clean_ms,
        ) = self._snapshot_expected_times(self.scheduler.estimator.name)
        self.is_running = True
        logger.info("practice: started session=%s estimator=%s",
                     self.session_id[:8], self.scheduler.estimator.name)

    def stop(self) -> None:
        self.is_running = False
        self.db.end_session(
            self.session_id, self.segments_attempted, self.segments_completed
        )
        logger.info("practice: stopped session=%s attempted=%d completed=%d",
                     self.session_id[:8], self.segments_attempted, self.segments_completed)

    def receive_result(self, event: AttemptResultEvent) -> None:
        """Called by SessionManager.route_event when attempt_result arrives."""
        # Clear the armed flag FIRST so any Death event arriving in the
        # same route_event batch doesn't try to reload on a finished attempt.
        self._current_state_path = None
        self._result_data = event
        self._result_event.set()

    def receive_event_attempt(self, event: EventAttemptEmission) -> None:
        """Persist one died/survived event row mid-attempt.

        Episode-level model updates still happen at ``receive_result`` time
        through ``scheduler.update_state_after_episode`` — this method only
        writes the raw event row. The Phase 1 segments-model adapter will
        consume these rows for live per-event fits; for now they shadow
        the legacy adapter's roll-up so the existing estimators stay
        numerically identical.
        """
        outcome = (
            AttemptOutcome.DIED if event.outcome == "died"
            else AttemptOutcome.SURVIVED
        )
        record = EventAttempt(
            segment_id=event.segment_id,
            episode_id=event.episode_id,
            outcome=outcome,
            time_ms=event.time_ms,
            session_id=self.session_id,
            source=AttemptSource.PRACTICE,
            chosen_allocator=self._last_allocator,
            is_hot=False,  # Practice always loads from a savestate → cold spawn.
        )
        self.db.log_event_attempt(record)
        self._current_episode_id = event.episode_id

    async def handle_death(self) -> None:
        """Reload the segment's start state so the player retries from the
        segment boundary, not from wherever they respawned.

        No-op when not currently armed (between attempts).
        """
        if self.paused:
            logger.info("practice: death ignored — paused")
            return
        path = self._current_state_path
        if not path:
            logger.info(
                "practice: death ignored — not armed (segment=%s)",
                self.current_segment_id,
            )
            return
        logger.info("practice: death → reloading state=%s", path)
        try:
            await self.emu.load_state(path)
        except Exception:
            logger.exception("practice: load_state on death failed (path=%s)", path)

    async def handle_level_exit_abort(self) -> None:
        """SessionManager calls this on LevelExit(goal='abort') during PRACTICE.

        Pit-falls / death-falls in SMW don't fire a Death frame (player_anim
        skips 9 entirely); they only show up as LevelExit. Same reload as
        a regular Death.
        """
        await self.handle_death()

    async def toggle_pause(self) -> None:
        """R-menu pause toggle. Pause drops the in-flight attempt and freezes
        the session clock; resume reloads the same segment fresh.

        Only meaningful mid-attempt: _current_state_path is non-None exactly
        while an attempt is armed (set in run_one, cleared in receive_result).
        """
        if not self.is_running or self._current_state_path is None:
            logger.info("practice: pause ignored — no attempt in flight")
            return
        if not self.paused:
            # PLAYING -> PAUSE: disarm backend timing (drops the attempt), freeze
            # the clock. The game keeps running; handle_death no-ops while paused.
            self.paused = True
            self.paused_at_epoch = time.time()
            await self.emu.send_command(PracticePauseCmd())
            logger.info("practice: paused (segment=%s)", self.current_segment_id)
        else:
            # PAUSED -> RESUME: fold the paused span into the offset, then reload
            # the SAME segment fresh (re-arms a new attempt via _on_practice_load).
            if self.paused_at_epoch is not None:
                self.pause_offset_sec += time.time() - self.paused_at_epoch
            self.paused = False
            self.paused_at_epoch = None
            if self._current_load_cmd is not None:
                await self.emu.send_command(self._current_load_cmd)
            logger.info("practice: resumed (segment=%s)", self.current_segment_id)

    async def run_one(self) -> bool:
        """Run one pick-send-receive cycle. Returns False if no segments available."""
        picked = self.scheduler.pick_next()
        if picked is None:
            logger.info("practice: no segments available — ending loop")
            return False

        self._last_allocator = self.scheduler.last_chosen_allocator

        expected_time_ms = None
        sel_out = picked.model_outputs.get(picked.selected_model)
        if sel_out and sel_out.total.expected_ms is not None and sel_out.total.expected_ms > 0:
            expected_time_ms = int(sel_out.total.expected_ms)

        label = picked.description
        if not label:
            start = "start" if picked.start_type == "entrance" else f"cp{picked.start_ordinal}"
            end = "goal" if picked.end_type == "goal" else f"cp{picked.end_ordinal}"
            label = f"L{picked.level_number} {start} > {end}"

        assert picked.state_path is not None  # scheduler only picks segments with save states
        cmd = SegmentCommand(
            id=picked.segment_id,
            state_path=picked.state_path,
            description=label,
            end_type=picked.end_type,
            expected_time_ms=expected_time_ms,
            auto_advance_delay_ms=self.auto_advance_delay_ms,
            death_penalty_ms=self.death_penalty_ms,
        )

        self.current_segment_id = cmd.id
        # Arm reload-on-death — set BEFORE the cmd send so an immediate
        # Death event isn't dropped while the load is mid-flight.
        self._current_state_path = cmd.state_path
        logger.info("practice: loading segment=%s label=%r state=%s",
                     cmd.id, label, cmd.state_path)

        self._current_load_cmd = PracticeLoadCmd(
            id=cmd.id,
            state_path=cmd.state_path,
            description=cmd.description,
            end_type=cmd.end_type,
            expected_time_ms=cmd.expected_time_ms,
            auto_advance_delay_ms=cmd.auto_advance_delay_ms,
            death_penalty_ms=cmd.death_penalty_ms,
        )
        await self.emu.send_command(self._current_load_cmd)

        # The segment is now the current one; broadcast so the dashboard's
        # live practice card renders before any attempt result lands.
        if self.on_segment_load is not None:
            self.on_segment_load(cmd.id)

        # Wait for attempt_result via receive_result() (set by SessionManager)
        self._result_event.clear()
        self._result_data = None

        load_timeouts = 0
        while self.is_running and self.emu.is_connected:
            try:
                await asyncio.wait_for(self._result_event.wait(), timeout=SEGMENT_LOAD_TIMEOUT_S)
                break
            except asyncio.TimeoutError:
                load_timeouts += 1
                if load_timeouts == 1:
                    log.info(
                        logger, "practice: waiting for attempt result",
                        segment_id=cmd.id, timeout_s=SEGMENT_LOAD_TIMEOUT_S,
                    )
                continue

        if self._result_data is not None:
            self._process_result(self._result_data)
        else:
            log.info(
                logger, "practice: attempt loop exited without result",
                segment_id=cmd.id,
                is_running=self.is_running,
                emu_connected=self.emu.is_connected,
                load_timeouts=load_timeouts,
            )

        self.current_segment_id = None
        return True

    def _process_result(self, result: AttemptResultEvent) -> None:
        attempt = Attempt(
            segment_id=result.segment_id,
            session_id=self.session_id,
            completed=result.completed,
            time_ms=result.time_ms,
            deaths=result.deaths,
            clean_tail_ms=result.clean_tail_ms,
            source=AttemptSource.PRACTICE,
            chosen_allocator=self._last_allocator,
        )
        # Production path: events were persisted as they arrived via
        # receive_event_attempt — we know because _current_episode_id was
        # set. Just update estimator state from the now-persisted episode.
        #
        # Test fallback path: when a caller delivers an AttemptResultEvent
        # without prior EventAttemptEmissions (unit tests that mock the
        # event pipeline), _current_episode_id is None — go through
        # record_attempt which uses the log_attempt shim to synthesize
        # event rows AND update model state in one call.
        if self._current_episode_id is None:
            self.scheduler.record_attempt(attempt)
        else:
            self.scheduler.update_state_after_episode(result.segment_id)
        self._current_episode_id = None
        self.segments_attempted += 1
        if result.completed:
            self.segments_completed += 1
        logger.info("practice: attempt segment=%s completed=%s time=%s deaths=%d",
                     result.segment_id, result.completed,
                     result.time_ms, result.deaths)
        if self.on_attempt:
            self.on_attempt(attempt)

    async def run_loop(self) -> None:
        """Run the full practice loop until stopped or no splits."""
        self.start()
        try:
            while self.is_running and self.emu.is_connected:
                if not await self.run_one():
                    break
        finally:
            try:
                await self.emu.send_command(PracticeStopCmd())
            except (ConnectionError, OSError) as exc:
                log.info(
                    logger, "practice teardown after backend disconnect",
                    exc=exc,
                )
            self.stop()
