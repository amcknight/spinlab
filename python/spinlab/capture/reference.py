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
    NoHotVariantError,
    NotConnectedError,
    NotInReferenceError,
    NotReplayingError,
    PracticeActiveError,
    ReferenceActiveError,
    RunPendingError,
    SessionDeleteAfterFinalizeError,
)
from ..models import (
    ActionResult, Attempt, AttemptSource, Mode, Status,
)
from ..protocol import (
    SPEED_UNCAPPED,
    CheckpointEvent,
    DeathEvent,
    FillGapLoadCmd,
    LevelEntranceEvent,
    LevelExitEvent,
    RecSavedEvent,
    ReferenceStartCmd,
    ReferenceStopCmd,
    ReplayCmd,
    ReplayStopCmd,
    SpawnEvent,
)
from .recorder import SegmentRecorder

if TYPE_CHECKING:
    from ..db import Database
    from ..scheduler import Scheduler
    from ..tcp_manager import TcpManager

logger = logging.getLogger(__name__)


def _seed_reference_attempts(
    db: "Database", capture_run_id: str, timing_rows: list[dict],
) -> int:
    """Insert seed attempts from drained recorded_segment_times rows. Returns count."""
    if not timing_rows:
        return 0
    now = _dt.now(UTC)
    count = 0
    for row in timing_rows:
        attempt = Attempt(
            segment_id=row["segment_id"],
            session_id=capture_run_id,
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

    def __init__(self, db: "Database", tcp: "TcpManager") -> None:
        self.db = db
        self.tcp = tcp
        self.recorder = SegmentRecorder()
        self.fill_gap_segment_id: str | None = None
        self._fill_gap_waypoint_id: str | None = None
        self.condition_registry: ConditionRegistry = ConditionRegistry()

        # Paused-run state (set by recovery or by stopping a session)
        self.paused_run_id: str | None = None

    def set_condition_registry(self, registry: ConditionRegistry) -> None:
        self.condition_registry = registry

    @property
    def has_paused_run(self) -> bool:
        return self.paused_run_id is not None

    @property
    def current_capture_session_id(self) -> str | None:
        return self.recorder.current_capture_session_id

    @property
    def rec_path(self) -> str | None:
        return self.recorder.rec_path

    def get_paused_state(self) -> dict | None:
        """Snapshot of the paused run for state_builder. None if no paused run."""
        if not self.paused_run_id:
            return None
        seg_count = self.db.conn.execute(
            "SELECT COUNT(*) FROM segments WHERE reference_id = ? AND active = 1",
            (self.paused_run_id,),
        ).fetchone()[0]
        sessions = self.db.list_capture_sessions_for_run(self.paused_run_id)
        return {
            "run_id": self.paused_run_id,
            "segments_captured": seg_count,
            "session_count": len(sessions),
        }

    def clear_and_idle(self) -> None:
        """Clear all in-memory state. Caller sets mode to IDLE."""
        self.recorder.clear()
        self.paused_run_id = None

    # ---------------------------------------------------------------- helpers

    def _game_rec_dir(self, data_dir: Path, game_id: str) -> Path:
        d = data_dir / game_id / "rec"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _new_session_spinrec_path(
        self, data_dir: Path, game_id: str, run_id: str, ordinal: int,
    ) -> str:
        path = self._game_rec_dir(data_dir, game_id) / f"{run_id}__sess{ordinal:03d}.spinrec"
        return str(path.resolve())

    def _end_current_session(self, end_reason: str) -> None:
        """End the current capture session (if any). Run remains draft=1.

        Called from: stop_reference, handle_disconnect, stop_replay,
        handle_replay_finished, handle_replay_error.
        """
        sess_id = self.recorder.current_capture_session_id
        run_id = self.recorder.capture_run_id
        if sess_id:
            self.db.end_capture_session(sess_id, end_reason=end_reason)
            logger.info("session: ended sess=%s reason=%s", sess_id, end_reason)
        # Surface run as paused (only if we had a run and it's still draft=1)
        if run_id:
            row = self.db.conn.execute(
                "SELECT draft FROM capture_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if row and row[0] == 1:
                self.paused_run_id = run_id
        self.recorder.clear()

    def _create_new_session(self, run_id: str, data_dir: Path, game_id: str) -> tuple[str, str]:
        """Create a new capture_session row + spinrec path. Returns (session_id, spinrec_path)."""
        next_ord = self.db.max_session_ordinal_for_run(run_id) + 1
        sess_id = f"sess_{uuid.uuid4().hex[:8]}"
        spinrec_path = self._new_session_spinrec_path(data_dir, game_id, run_id, next_ord)
        self.db.create_capture_session(
            session_id=sess_id, capture_run_id=run_id,
            ordinal=next_ord, spinrec_path=spinrec_path,
        )
        logger.info("session: created sess=%s run=%s ordinal=%d", sess_id, run_id, next_ord)
        return sess_id, spinrec_path

    # ---------------------------------------------------------------- start/resume

    async def start_reference(
        self, mode: Mode,
        game_id: str, data_dir: Path, run_name: str | None = None,
    ) -> ActionResult:
        if self.paused_run_id:
            raise RunPendingError()
        if mode == Mode.PRACTICE:
            raise PracticeActiveError()
        if mode == Mode.REPLAY:
            raise AlreadyReplayingError()
        if not self.tcp.is_connected:
            raise NotConnectedError()

        self.recorder.clear()
        run_id = f"live_{uuid.uuid4().hex[:8]}"
        run_name = run_name or f"Live {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M')}"
        self.db.create_capture_run(run_id, game_id, run_name, draft=True)
        sess_id, spinrec_path = self._create_new_session(run_id, data_dir, game_id)

        self.recorder.capture_run_id = run_id
        self.recorder.current_capture_session_id = sess_id
        self.paused_run_id = None  # we're now actively recording

        logger.info("reference: started run=%s name=%r", run_id, run_name)
        await self.tcp.send_command(ReferenceStartCmd(path=spinrec_path))
        return ActionResult(status=Status.STARTED, new_mode=Mode.REFERENCE)

    async def resume_reference(
        self, mode: Mode, game_id: str, data_dir: Path,
    ) -> ActionResult:
        if not self.paused_run_id:
            raise NotInReferenceError()
        if mode == Mode.PRACTICE:
            raise PracticeActiveError()
        if mode == Mode.REPLAY:
            raise AlreadyReplayingError()
        if not self.tcp.is_connected:
            raise NotConnectedError()

        run_id = self.paused_run_id
        sess_id, spinrec_path = self._create_new_session(run_id, data_dir, game_id)
        self.recorder.capture_run_id = run_id
        self.recorder.current_capture_session_id = sess_id
        self.paused_run_id = None

        logger.info("reference: resumed run=%s sess=%s", run_id, sess_id)
        await self.tcp.send_command(ReferenceStartCmd(path=spinrec_path))
        return ActionResult(status=Status.STARTED, new_mode=Mode.REFERENCE)

    # ---------------------------------------------------------------- stop/finalize/discard

    async def stop_reference(self, mode: Mode) -> ActionResult:
        if mode != Mode.REFERENCE:
            raise NotInReferenceError()
        if self.tcp.is_connected:
            await self.tcp.send_command(ReferenceStopCmd())
        seg_count_in_run = self.db.conn.execute(
            "SELECT COUNT(*) FROM segments WHERE reference_id = ?",
            (self.recorder.capture_run_id,),
        ).fetchone()[0] if self.recorder.capture_run_id else 0
        logger.info("reference: stopped — %d total segments in run", seg_count_in_run)
        self._end_current_session(end_reason="stopped")
        return ActionResult(status=Status.STOPPED, new_mode=Mode.IDLE)

    async def finalize_run(self, name: str, scheduler: "Scheduler | None" = None) -> ActionResult:
        if not self.paused_run_id:
            raise NotInReferenceError()
        run_id = self.paused_run_id
        timing_rows = self.db.drain_recorded_segment_times_for_run(run_id)
        self.db.promote_draft(run_id, name)
        self.db.set_active_capture_run(run_id)
        seeded = _seed_reference_attempts(self.db, run_id, timing_rows)
        if seeded and scheduler:
            scheduler.rebuild_all_states()
        self.paused_run_id = None
        logger.info("reference: finalized run=%s as %r (seeded %d attempts)",
                     run_id, name, seeded)
        return ActionResult(status=Status.OK)

    async def save_and_finish_run(
        self, mode: Mode, name: str, scheduler: "Scheduler | None" = None,
    ) -> ActionResult:
        """Combined Stop Session + Finalize, atomic. Single-session ergonomics."""
        if mode != Mode.REFERENCE:
            raise NotInReferenceError()
        if self.tcp.is_connected:
            await self.tcp.send_command(ReferenceStopCmd())
        with self.db.transaction():
            self._end_current_session(end_reason="stopped")
            run_id = self.paused_run_id
            if not run_id:
                raise NotInReferenceError()
            timing_rows = self.db.drain_recorded_segment_times_for_run(run_id)
            self.db.promote_draft(run_id, name)
            self.db.set_active_capture_run(run_id)
            seeded = _seed_reference_attempts(self.db, run_id, timing_rows)
        if seeded and scheduler:
            scheduler.rebuild_all_states()
        self.paused_run_id = None
        logger.info("reference: save_and_finish run=%s as %r (seeded %d attempts)",
                     run_id, name, seeded)
        return ActionResult(status=Status.OK, new_mode=Mode.IDLE)

    async def discard_run(self) -> ActionResult:
        if not self.paused_run_id:
            raise NotInReferenceError()
        run_id = self.paused_run_id
        self.db.hard_delete_capture_run(run_id)
        self.paused_run_id = None
        logger.info("reference: discarded run=%s", run_id)
        return ActionResult(status=Status.OK)

    async def delete_capture_session(self, session_id: str) -> ActionResult:
        """Delete a single capture session. Only allowed while run is paused."""
        sess = self.db.get_capture_session(session_id)
        if not sess:
            raise NotInReferenceError()
        run_id = sess["capture_run_id"]
        row = self.db.conn.execute(
            "SELECT draft FROM capture_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if not row or row[0] != 1:
            raise SessionDeleteAfterFinalizeError()
        try:
            Path(sess["spinrec_path"]).unlink(missing_ok=True)
        except OSError:
            pass
        self.db.delete_capture_session(session_id)
        logger.info("session: deleted sess=%s from run=%s", session_id, run_id)
        return ActionResult(status=Status.OK)

    # ---------------------------------------------------------------- replay

    async def start_replay(
        self, mode: Mode,
        game_id: str, spinrec_path: str, speed: int = SPEED_UNCAPPED,
    ) -> ActionResult:
        if self.paused_run_id:
            raise RunPendingError()
        if mode == Mode.PRACTICE:
            raise PracticeActiveError()
        if mode == Mode.REFERENCE:
            raise ReferenceActiveError()
        if mode == Mode.REPLAY:
            raise AlreadyReplayingError()
        if not self.tcp.is_connected:
            raise NotConnectedError()

        # Replay creates its own ephemeral capture_run + session for capture machinery
        self.recorder.clear()
        run_id = f"replay_{uuid.uuid4().hex[:8]}"
        run_name = f"Replay {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M')}"
        self.db.create_capture_run(run_id, game_id, run_name, draft=True)
        sess_id = f"sess_{uuid.uuid4().hex[:8]}"
        self.db.create_capture_session(
            session_id=sess_id, capture_run_id=run_id,
            ordinal=1, spinrec_path=spinrec_path,
        )
        self.recorder.capture_run_id = run_id
        self.recorder.current_capture_session_id = sess_id

        await self.tcp.send_command(ReplayCmd(path=spinrec_path, speed=speed))
        return ActionResult(status=Status.STARTED, new_mode=Mode.REPLAY)

    async def stop_replay(self, mode: Mode) -> ActionResult:
        if mode != Mode.REPLAY:
            raise NotReplayingError()
        if self.tcp.is_connected:
            await self.tcp.send_command(ReplayStopCmd())
        run_id = self.recorder.capture_run_id
        seg_count = self.db.conn.execute(
            "SELECT COUNT(*) FROM segments WHERE reference_id = ?", (run_id,),
        ).fetchone()[0] if run_id else 0
        if seg_count > 0:
            self._end_current_session(end_reason="stopped")
        else:
            self._end_current_session(end_reason="stopped")
            if run_id:
                self.db.hard_delete_capture_run(run_id)
                self.paused_run_id = None
        return ActionResult(status=Status.STOPPED, new_mode=Mode.IDLE)

    # ---------------------------------------------------------------- fill_gap (unchanged behaviour)

    async def start_fill_gap(self, segment_id: str) -> ActionResult:
        if not self.tcp.is_connected:
            raise NotConnectedError()
        row = self.db.conn.execute(
            "SELECT start_waypoint_id FROM segments WHERE id = ?", (segment_id,)
        ).fetchone()
        start_waypoint_id = row[0] if row else None
        hot = (self.db.get_save_state(start_waypoint_id, "hot")
               if start_waypoint_id else None)
        if not hot:
            raise NoHotVariantError()
        self.fill_gap_segment_id = segment_id
        self._fill_gap_waypoint_id = start_waypoint_id
        await self.tcp.send_command(FillGapLoadCmd(state_path=hot.state_path, message="Die to capture cold start"))
        return ActionResult(status=Status.STARTED, new_mode=Mode.FILL_GAP)

    def handle_fill_gap_spawn(self, event: SpawnEvent) -> bool:
        if not event.state_captured or not self.fill_gap_segment_id:
            return False
        waypoint_id = self._fill_gap_waypoint_id
        if waypoint_id:
            from ..models import WaypointSaveState
            self.db.add_save_state(WaypointSaveState(
                waypoint_id=waypoint_id,
                variant_type="cold",
                state_path=event.state_path,
                is_default=True,
            ))
        self.fill_gap_segment_id = None
        self._fill_gap_waypoint_id = None
        return True

    # ---------------------------------------------------------------- event routing

    def handle_entrance(self, event: LevelEntranceEvent) -> None:
        logger.info("capture: entrance level=%s", event.level)
        self.recorder.handle_entrance(event)

    def handle_checkpoint(self, event: CheckpointEvent, game_id: str) -> None:
        logger.info("capture: checkpoint level=%s cp=%s",
                     event.level_num, event.cp_ordinal)
        self.recorder.handle_checkpoint(event, game_id, self.db,
                                           self.condition_registry)

    def handle_death(self, event: DeathEvent) -> None:
        self.recorder.died = True
        self.recorder.handle_death(timestamp_ms=None)

    def handle_spawn(self, event: SpawnEvent, game_id: str) -> None:
        logger.info("capture: spawn level=%s state_captured=%s",
                     event.level_num, event.state_captured)
        self.recorder.handle_spawn_timing(timestamp_ms=None)
        self.recorder.handle_spawn(event, game_id, self.db,
                                      self.condition_registry)

    def handle_exit(self, event: LevelExitEvent, game_id: str) -> None:
        logger.info("capture: exit level=%s", event.level)
        self.recorder.handle_exit(event, game_id, self.db,
                                     self.condition_registry)

    def handle_rec_saved(self, event: RecSavedEvent) -> None:
        self.recorder.rec_path = event.path

    def handle_replay_finished(self) -> None:
        self._end_current_session(end_reason="stopped")

    def handle_replay_error(self) -> None:
        run_id = self.recorder.capture_run_id
        seg_count = self.db.conn.execute(
            "SELECT COUNT(*) FROM segments WHERE reference_id = ?", (run_id,),
        ).fetchone()[0] if run_id else 0
        self._end_current_session(end_reason="replay_error")
        if seg_count == 0 and run_id:
            self.db.hard_delete_capture_run(run_id)
            self.paused_run_id = None

    def handle_disconnect(self) -> None:
        """Treat as a clean session end. Run stays paused for resume."""
        self._end_current_session(end_reason="disconnected")

    # ---------------------------------------------------------------- recovery

    def recover_paused_run(self, game_id: str) -> None:
        """On game-load, find any paused run for this game and surface it."""
        run_id = self.db.recover_paused_capture_run(game_id)
        self.paused_run_id = run_id
        if run_id:
            logger.info("recovery: paused run loaded id=%s", run_id)
