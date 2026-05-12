"""ReferenceController — orchestrates reference recording and replay capture.

State model:
- IDLE: no run loaded
- RECORDING: a session is active, recorder is buffering events
- PAUSED: a draft=1 capture_run exists but no active session

Stop is non-destructive: it ends the current session and leaves the run paused.
Resume creates a new session under the existing paused run. Finalize drains
recorded_segment_times into attempts and sets draft=0.
"""
from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from datetime import datetime as _dt
from pathlib import Path
from typing import TYPE_CHECKING

from ..condition_registry import ConditionRegistry
from ..errors import (
    AlreadyReplayingError,
    DraftPendingError,
    NoPausedRunError,
    NotConnectedError,
    NotInReferenceError,
    NotReplayingError,
    PracticeActiveError,
    ReferenceActiveError,
    SessionDeleteAfterFinalizeError,
    SessionInUseError,
)
from ..models import (
    ActionResult,
    Attempt,
    AttemptSource,
    Mode,
    Status,
)
from ..protocol import (
    SPEED_UNCAPPED,
    CheckpointEvent,
    DeathEvent,
    LevelEntranceEvent,
    LevelExitEvent,
    ReferenceStartCmd,
    ReferenceStopCmd,
    ReplayCmd,
    ReplayStopCmd,
    SpawnEvent,
)
from .recorder import SegmentRecorder

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ..db import Database
    from ..db.recorded_segment_times import RecordedSegmentTimeRow
    from ..emu_backend import EmuBackend
    from ..scheduler import Scheduler

logger = logging.getLogger(__name__)


def _seed_reference_attempts(
    db: "Database", capture_run_id: str, timing_rows: "Sequence[RecordedSegmentTimeRow]",
) -> int:
    """Insert seed attempts from drained recorded_segment_times rows. Returns count."""
    if not timing_rows:
        return 0
    now = _dt.now(UTC)
    count = 0
    for row in timing_rows:
        attempt = Attempt(
            segment_id=row["segment_id"],
            parent_id=capture_run_id,
            completed=True,
            time_ms=row["time_ms"],
            deaths=row["deaths"],
            clean_tail_ms=row["clean_tail_ms"],
            source=AttemptSource.REFERENCE,
            created_at=now,
        )
        db.log_attempt(attempt)
        count += 1
        logger.info("seed: segment=%s time=%dms deaths=%d clean_tail=%dms",
                     row["segment_id"], row["time_ms"], row["deaths"],
                     row["clean_tail_ms"])
    return count


class ReferenceController:
    """Manages reference/replay capture, sessions, and finalize/discard."""

    def __init__(self, db: "Database", emu: "EmuBackend") -> None:
        self.db = db
        self.emu = emu
        self.recorder = SegmentRecorder()
        self.condition_registry: ConditionRegistry = ConditionRegistry()

        # `paused_run_id` and `recorder.capture_run_id` form a mutually
        # exclusive pair: at most one is set at any time, encoding the run
        # phase (idle / recording / paused). Mutate ONLY through
        # `_enter_recording` / `_enter_paused` / `_enter_idle`, which assert
        # the invariant after each transition.
        self.paused_run_id: str | None = None

    def set_condition_registry(self, registry: ConditionRegistry) -> None:
        self.condition_registry = registry

    @property
    def has_paused_run(self) -> bool:
        return self.paused_run_id is not None

    @property
    def is_recording(self) -> bool:
        return self.recorder.capture_run_id is not None

    @property
    def active_run_id(self) -> str | None:
        """Run id of the currently recording OR paused run; None when idle.

        Single read API for callers that don't need to distinguish phases —
        e.g., StateBuilder building snapshots, queries that just need 'the
        run we're working on right now'.
        """
        return self.recorder.capture_run_id or self.paused_run_id

    @property
    def current_capture_session_id(self) -> str | None:
        return self.recorder.current_capture_session_id

    @property
    def rec_path(self) -> str | None:
        return self.recorder.rec_path

    # These three methods are the only places that mutate the
    # paused_run_id / recorder.capture_run_id pair. Everywhere else reads
    # but does not write. Keep it that way.

    def _assert_run_state_invariant(self) -> None:
        if self.paused_run_id and self.recorder.capture_run_id:
            raise AssertionError(
                f"ReferenceController invariant violated: paused_run_id="
                f"{self.paused_run_id!r} and recorder.capture_run_id="
                f"{self.recorder.capture_run_id!r} are both set."
            )

    def _enter_recording(self, run_id: str, session_id: str) -> None:
        """Transition to RECORDING phase: arm the recorder, clear paused state."""
        self.paused_run_id = None
        self.recorder.capture_run_id = run_id
        self.recorder.current_capture_session_id = session_id
        self._assert_run_state_invariant()

    def _enter_paused(self, run_id: str) -> None:
        """Transition to PAUSED phase: clear recorder first, then surface the run.

        Order matters: clearing the recorder before assigning paused_run_id
        prevents a window where both fields are set.
        """
        self.recorder.clear()
        self.paused_run_id = run_id
        self._assert_run_state_invariant()

    def _enter_idle(self) -> None:
        """Transition to IDLE phase: clear all run state."""
        self.recorder.clear()
        self.paused_run_id = None
        self._assert_run_state_invariant()

    def get_paused_state(self) -> dict | None:
        """Snapshot of the paused run for state_builder. None if no paused run."""
        if not self.paused_run_id:
            return None
        seg_count = self.db.count_segments_for_run(
            self.paused_run_id, active_only=True,
        )
        sessions = self.db.list_capture_sessions_for_run(self.paused_run_id)
        return {
            "run_id": self.paused_run_id,
            "segments_captured": seg_count,
            "session_count": len(sessions),
        }

    def clear_and_idle(self) -> None:
        """Clear all in-memory state. Caller sets mode to IDLE."""
        self._enter_idle()

    def _game_rec_dir(self, data_dir: Path, game_id: str) -> Path:
        d = data_dir / game_id / "rec"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _end_current_session(self, end_reason: str) -> None:
        """End the current capture session (if any). Run remains draft=1.

        Called from: stop_reference, handle_disconnect, stop_replay,
        handle_replay_finished, handle_replay_error.
        """
        sess_id = self.recorder.current_capture_session_id
        run_id = self.recorder.capture_run_id
        if sess_id:
            sess_row = self.db.get_capture_session(sess_id)
            seg_count = self.db.count_segments_for_capture_session(sess_id)
            self.db.end_capture_session(sess_id, end_reason=end_reason)
            # Compute duration from started_at→now using the row we just fetched.
            duration_s: float | None = None
            if sess_row and sess_row.get("started_at"):
                started = datetime.fromisoformat(sess_row["started_at"])
                duration_s = (datetime.now(UTC) - started).total_seconds()
            ordinal = sess_row["ordinal"] if sess_row else "?"
            dur_str = f"{duration_s:.1f}" if duration_s is not None else "?"
            logger.info(
                "session: ended sess=%s ordinal=%s duration_s=%s segments=%d reason=%s",
                sess_id, ordinal, dur_str, seg_count, end_reason,
            )
        # If we had a run and it's still draft=1, surface it as paused;
        # otherwise drop to idle. _enter_paused / _enter_idle clear the
        # recorder atomically.
        should_pause = bool(run_id and self.db.is_run_draft(run_id))
        if should_pause and run_id:
            self._enter_paused(run_id)
        else:
            self._enter_idle()

    def _create_new_session(self, run_id: str, data_dir: Path, game_id: str) -> tuple[str, int]:
        """Create a new capture_session row. Returns (session_id, ordinal)."""
        next_ord = self.db.max_session_ordinal_for_run(run_id) + 1
        sess_id = f"sess_{uuid.uuid4().hex[:8]}"
        self.db.create_capture_session(
            session_id=sess_id, capture_run_id=run_id,
            ordinal=next_ord,
        )
        logger.info("session: created sess=%s run=%s ordinal=%d", sess_id, run_id, next_ord)
        return sess_id, next_ord

    async def start_reference(
        self, mode: Mode,
        game_id: str, data_dir: Path, run_name: str | None = None,
    ) -> ActionResult:
        if self.paused_run_id:
            raise DraftPendingError()
        if mode == Mode.PRACTICE:
            raise PracticeActiveError()
        if mode == Mode.REPLAY:
            raise AlreadyReplayingError()
        if not self.emu.is_connected:
            raise NotConnectedError()

        self._enter_idle()
        run_id = f"live_{uuid.uuid4().hex[:8]}"
        run_name = run_name or f"Live {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M')}"
        self.db.create_capture_run(run_id, game_id, run_name, draft=True)
        sess_id, ordinal = self._create_new_session(run_id, data_dir, game_id)
        replay_path = self._game_rec_dir(data_dir, game_id) / f"{run_id}__sess{ordinal:03d}.replay"

        self._enter_recording(run_id, sess_id)

        logger.info("reference: started run=%s name=%r", run_id, run_name)
        await self.emu.send_command(ReferenceStartCmd(path=str(replay_path)))
        return ActionResult(status=Status.STARTED, new_mode=Mode.REFERENCE)

    async def resume_reference(
        self, mode: Mode, game_id: str, data_dir: Path,
    ) -> ActionResult:
        if not self.paused_run_id:
            raise NoPausedRunError()
        if mode == Mode.PRACTICE:
            raise PracticeActiveError()
        if mode == Mode.REPLAY:
            raise AlreadyReplayingError()
        if not self.emu.is_connected:
            raise NotConnectedError()

        run_id = self.paused_run_id
        sess_id, ordinal = self._create_new_session(run_id, data_dir, game_id)
        replay_path = self._game_rec_dir(data_dir, game_id) / f"{run_id}__sess{ordinal:03d}.replay"
        self._enter_recording(run_id, sess_id)

        logger.info("reference: resumed run=%s sess=%s", run_id, sess_id)
        await self.emu.send_command(ReferenceStartCmd(path=str(replay_path)))
        return ActionResult(status=Status.STARTED, new_mode=Mode.REFERENCE)

    async def stop_reference(self, mode: Mode) -> ActionResult:
        if mode != Mode.REFERENCE:
            raise NotInReferenceError()
        if self.emu.is_connected:
            await self.emu.send_command(ReferenceStopCmd())
        seg_count_in_run = (
            self.db.count_segments_for_run(self.recorder.capture_run_id)
            if self.recorder.capture_run_id else 0
        )
        logger.info("reference: stopped — %d total segments in run", seg_count_in_run)
        self._end_current_session(end_reason="stopped")
        return ActionResult(status=Status.STOPPED, new_mode=Mode.IDLE)

    async def finalize_run(self, name: str, scheduler: "Scheduler | None" = None) -> ActionResult:
        if not self.paused_run_id:
            raise NoPausedRunError()
        run_id = self.paused_run_id
        timing_rows = self.db.drain_recorded_segment_times_for_run(run_id)
        self.db.promote_draft(run_id, name)
        self.db.set_active_capture_run(run_id)
        seeded = _seed_reference_attempts(self.db, run_id, timing_rows)
        # Always rebuild after activation: set_active_capture_run changed which
        # reference the scheduler should be reasoning about, regardless of whether
        # this finalize added new attempts.
        if scheduler:
            scheduler.rebuild_all_states()
        self._enter_idle()
        logger.info("reference: finalized run=%s as %r (seeded %d attempts)",
                     run_id, name, seeded)
        return ActionResult(status=Status.OK)

    async def save_and_finish_run(
        self, mode: Mode, name: str, scheduler: "Scheduler | None" = None,
    ) -> ActionResult:
        """Combined Stop Session + Finalize, atomic.

        Two valid entry conditions:
          - mode == REFERENCE: full atomic stop + finalize.
          - mode == IDLE and paused_run_id is set: the user already clicked
            Stop separately; just finalize the paused run. Delegates to
            finalize_run so the dashboard's primary "Save & Finish Run"
            button works regardless of whether the user clicked Stop first.

        Inlines mutations on ``db.conn`` inside an explicit ``BEGIN IMMEDIATE``
        because the mixin methods each call ``conn.commit()`` internally —
        calling them inside an outer transaction would commit partial work and
        break atomicity. Either every step (end session → drain timing rows →
        promote draft → set active → seed attempts) succeeds, or rollback
        leaves every row exactly as it was. The stop command is sent before the
        transaction since it is non-transactional; recorder state is cleared
        only on successful commit via ``_enter_idle``.
        """
        # Already-stopped case: just promote the draft. finalize_run handles
        # the lighter version (no recorder/session state to wind down).
        if mode == Mode.IDLE and self.paused_run_id:
            return await self.finalize_run(name, scheduler=scheduler)
        if mode != Mode.REFERENCE:
            raise NotInReferenceError()
        if self.emu.is_connected:
            await self.emu.send_command(ReferenceStopCmd())

        # Snapshot recorder state for the atomic block; do NOT clear it yet —
        # if the transaction rolls back we want the recorder still pointing at
        # the live session so the user can retry.
        sess_id = self.recorder.current_capture_session_id
        run_id = self.recorder.capture_run_id
        if not run_id:
            raise NoPausedRunError()

        from .finalizer import atomic_save_and_finish_run
        seeded = atomic_save_and_finish_run(self.db, run_id, sess_id, name)

        if scheduler:
            scheduler.rebuild_all_states()
        self._enter_idle()
        for attempt in seeded:
            logger.info("seed: segment=%s time=%dms deaths=%d clean_tail=%dms",
                         attempt.segment_id, attempt.time_ms, attempt.deaths,
                         attempt.clean_tail_ms)
        logger.info("reference: save_and_finish run=%s as %r (seeded %d attempts)",
                     run_id, name, len(seeded))
        return ActionResult(status=Status.OK, new_mode=Mode.IDLE)

    async def discard_run(self) -> ActionResult:
        if not self.paused_run_id:
            raise NoPausedRunError()
        run_id = self.paused_run_id
        self.db.hard_delete_capture_run(run_id)
        self._enter_idle()
        logger.info("reference: discarded run=%s", run_id)
        return ActionResult(status=Status.OK)

    async def delete_capture_session(self, session_id: str) -> ActionResult:
        """Delete a single capture session. Only allowed while run is paused and
        the session is not currently being recorded into."""
        if self.recorder.current_capture_session_id == session_id:
            raise SessionInUseError()
        sess = self.db.get_capture_session(session_id)
        if not sess:
            raise NotInReferenceError()
        run_id = sess["capture_run_id"]
        if not self.db.is_run_draft(run_id):
            raise SessionDeleteAfterFinalizeError()
        self.db.delete_capture_session(session_id)
        logger.info("session: deleted sess=%s from run=%s", session_id, run_id)
        return ActionResult(status=Status.OK)


    async def start_replay(
        self, mode: Mode,
        game_id: str, replay_path: str, speed: int = SPEED_UNCAPPED,
    ) -> ActionResult:
        if self.paused_run_id:
            raise DraftPendingError()
        if mode == Mode.PRACTICE:
            raise PracticeActiveError()
        if mode == Mode.REFERENCE:
            raise ReferenceActiveError()
        if mode == Mode.REPLAY:
            raise AlreadyReplayingError()
        if not self.emu.is_connected:
            raise NotConnectedError()

        # Replay creates its own ephemeral capture_run + session for capture machinery
        self._enter_idle()
        run_id = f"replay_{uuid.uuid4().hex[:8]}"
        run_name = f"Replay {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M')}"
        self.db.create_capture_run(run_id, game_id, run_name, draft=True)
        sess_id = f"sess_{uuid.uuid4().hex[:8]}"
        self.db.create_capture_session(
            session_id=sess_id, capture_run_id=run_id,
            ordinal=1,
        )
        self._enter_recording(run_id, sess_id)

        await self.emu.send_command(ReplayCmd(path=replay_path, speed=speed))
        return ActionResult(status=Status.STARTED, new_mode=Mode.REPLAY)

    async def stop_replay(self, mode: Mode) -> ActionResult:
        if mode != Mode.REPLAY:
            raise NotReplayingError()
        # Snapshot run_id BEFORE sending the command. send_command awaits and
        # yields control; the resulting ReplayFinishedEvent runs the event
        # loop's handle_replay_finished, which calls _end_current_session and
        # transitions the recorder to paused (if segments were captured) or
        # idle (if not). After that, recorder.capture_run_id is None — so
        # capturing it here is the only reliable way to know what run we
        # just ended.
        run_id = self.recorder.capture_run_id
        if self.emu.is_connected:
            await self.emu.send_command(ReplayStopCmd())
        # Do NOT call _end_current_session here: handle_replay_finished
        # already ran from the event flow and drove the paused/idle
        # transition. A second call would see an empty recorder and run
        # _enter_idle, wiping the paused_run_id the event handler just set.
        if run_id:
            seg_count = self.db.count_segments_for_run(run_id)
            if seg_count == 0:
                # Nothing captured — no value in keeping the run; delete it.
                # The event handler also drops to idle on no-segments via the
                # draft check, but we still need to delete the empty draft row.
                self.db.hard_delete_capture_run(run_id)
                self._enter_idle()
        # If segments were captured, the run stays paused so the user can finalize.
        # recover_paused_capture_run excludes replay_ IDs, so this won't clobber
        # real paused reference runs on the next dashboard restart.
        return ActionResult(status=Status.STOPPED, new_mode=Mode.IDLE)


    async def handle_entrance(self, event: LevelEntranceEvent) -> None:
        logger.info("capture: entrance level=%s", event.level)
        if self.is_recording:
            from spinlab.state_paths import segment_id_for_event
            seg_id = segment_id_for_event(event)
            if seg_id:
                try:
                    await self.emu.save_state(seg_id)
                except Exception:
                    logger.exception(
                        "save_state failed for entrance event seg_id=%r", seg_id,
                    )
        self.recorder.handle_entrance(event)

    async def handle_checkpoint(self, event: CheckpointEvent, game_id: str) -> None:
        logger.info("capture: checkpoint level=%s cp=%s",
                     event.level_num, event.cp_ordinal)
        if self.is_recording:
            from spinlab.state_paths import segment_id_for_event
            seg_id = segment_id_for_event(event)
            if seg_id:
                try:
                    await self.emu.save_state(seg_id)
                except Exception:
                    logger.exception(
                        "save_state failed for checkpoint event seg_id=%r", seg_id,
                    )
        self.recorder.handle_checkpoint(event, game_id, self.db,
                                           self.condition_registry)

    def handle_death(self, event: DeathEvent) -> None:
        self.recorder.died = True
        self.recorder.handle_death(timestamp_ms=None)

    def handle_spawn(self, event: SpawnEvent, game_id: str) -> None:
        logger.info("capture: spawn level=%s state_path=%s",
                     event.level_num, event.state_path)
        self.recorder.handle_spawn_timing(timestamp_ms=event.timestamp_ms)
        self.recorder.handle_spawn(event, game_id, self.db,
                                      self.condition_registry)

    def handle_exit(self, event: LevelExitEvent, game_id: str) -> None:
        logger.info("capture: exit level=%s", event.level)
        self.recorder.handle_exit(event, game_id, self.db,
                                     self.condition_registry)

    def handle_replay_finished(self) -> None:
        # End the session and leave the run paused — the user can finalize or discard.
        # recover_paused_capture_run excludes replay_ IDs, so this draft won't clobber
        # a real paused reference run on the next dashboard restart.
        self._end_current_session(end_reason="stopped")

    def handle_replay_error(self) -> None:
        run_id = self.recorder.capture_run_id
        seg_count = self.db.count_segments_for_run(run_id) if run_id else 0
        self._end_current_session(end_reason="replay_error")
        if run_id and seg_count == 0:
            # Errored with nothing captured — discard the empty run.
            self.db.hard_delete_capture_run(run_id)
            self._enter_idle()
        # If segments were captured before the error, leave as paused so the user
        # can decide whether to finalize or discard them.

    def handle_disconnect(self) -> None:
        """Treat as a clean session end. Run stays paused for resume."""
        self._end_current_session(end_reason="disconnected")


    def recover_paused_run(self, game_id: str) -> None:
        """On game-load, find any paused run for this game and surface it."""
        run_id = self.db.recover_paused_capture_run(game_id)
        if run_id:
            self._enter_paused(run_id)
            logger.info("recovery: paused run loaded id=%s", run_id)
