"""Tests for ReferenceController orchestration logic.

Uses a real SQLite Database (tmp_path) and FakeEmuBackend to exercise the
controller's real interactions with the DB schema and backend protocol.
Mocking both collaborators would reduce these to tautology tests.
"""
import pytest

from spinlab.capture import ReferenceController
from spinlab.db import Database
from spinlab.errors import (
    AlreadyReplayingError,
    DraftPendingError,
    NotConnectedError,
    NotInReferenceError,
    NotReplayingError,
    PracticeActiveError,
    ReferenceActiveError,
)
from spinlab.models import EndpointType, Mode, Segment, Status
from spinlab.protocol import (
    ReferenceStartCmd,
    ReferenceStopCmd,
    ReplayCmd,
)


@pytest.fixture
def db(tmp_path):
    """Real SQLite database, per-test."""
    d = Database(tmp_path / "test.db")
    d.upsert_game("g1", "Test Game", "any%")
    return d


@pytest.fixture
def controller(db, fake_emu):
    return ReferenceController(db, fake_emu)


class TestStartReference:
    async def test_guard_paused_run_pending(self, controller, tmp_path):
        controller.paused_run_id = "fake_paused_run"
        with pytest.raises(DraftPendingError):
            await controller.start_reference(Mode.IDLE, "g1", tmp_path, run_name="test")

    async def test_guard_practice_active(self, controller, tmp_path):
        with pytest.raises(PracticeActiveError):
            await controller.start_reference(Mode.PRACTICE, "g1", tmp_path)

    async def test_guard_already_replaying(self, controller, tmp_path):
        with pytest.raises(AlreadyReplayingError):
            await controller.start_reference(Mode.REPLAY, "g1", tmp_path)

    async def test_guard_not_connected(self, controller, tmp_path, fake_emu):
        fake_emu.is_connected = False
        with pytest.raises(NotConnectedError):
            await controller.start_reference(Mode.IDLE, "g1", tmp_path)

    async def test_happy_path(self, controller, tmp_path, fake_emu):
        result = await controller.start_reference(Mode.IDLE, "g1", tmp_path, run_name="my run")
        assert result.status == Status.STARTED
        assert result.new_mode == Mode.REFERENCE
        assert len(fake_emu.sent_commands) == 1
        assert isinstance(fake_emu.sent_commands[0], ReferenceStartCmd)
        assert controller.recorder.capture_run_id is not None


class TestStopReference:
    async def test_not_in_reference(self, controller):
        with pytest.raises(NotInReferenceError):
            await controller.stop_reference(Mode.IDLE)

    async def test_happy_path_pauses_run(self, controller, tmp_path, fake_emu, db):
        await controller.start_reference(Mode.IDLE, "g1", tmp_path)
        run_id = controller.recorder.capture_run_id

        result = await controller.stop_reference(Mode.REFERENCE)

        assert result.status == Status.STOPPED
        assert result.new_mode == Mode.IDLE
        stop_cmds = [c for c in fake_emu.sent_commands if isinstance(c, ReferenceStopCmd)]
        assert len(stop_cmds) == 1
        assert controller.paused_run_id == run_id
        row = db.conn.execute(
            "SELECT status FROM capture_runs WHERE id = ?", (run_id,)
        ).fetchone()
        assert row is not None and row[0] == "draft"


class TestStartReplay:
    async def test_guard_reference_active(self, controller):
        with pytest.raises(ReferenceActiveError):
            await controller.start_replay(Mode.REFERENCE, "g1", "/tmp/foo.spinrec")

    async def test_guard_already_replaying(self, controller):
        with pytest.raises(AlreadyReplayingError):
            await controller.start_replay(Mode.REPLAY, "g1", "/tmp/foo.spinrec")

    async def test_happy_path(self, controller, fake_emu):
        result = await controller.start_replay(Mode.IDLE, "g1", "/tmp/foo.spinrec", speed=2)
        assert result.status == Status.STARTED
        assert result.new_mode == Mode.REPLAY
        replay_cmds = [c for c in fake_emu.sent_commands if isinstance(c, ReplayCmd)]
        assert len(replay_cmds) == 1
        assert replay_cmds[0].path == "/tmp/foo.spinrec"
        assert replay_cmds[0].speed == 2


class TestStopReplay:
    async def test_not_replaying(self, controller):
        with pytest.raises(NotReplayingError):
            await controller.stop_replay(Mode.IDLE)

    async def test_no_segments_hard_deletes_run(self, controller, db, fake_emu):
        await controller.start_replay(Mode.IDLE, "g1", "/tmp/foo.spinrec")
        run_id = controller.recorder.capture_run_id

        result = await controller.stop_replay(Mode.REPLAY)

        assert result.status == Status.STOPPED
        row = db.conn.execute(
            "SELECT id FROM capture_runs WHERE id = ?", (run_id,)
        ).fetchone()
        assert row is None, "capture_run should have been hard-deleted"


class TestHandleReplayFinished:
    async def test_pauses_run_after_replay_finishes(self, controller, db):
        """handle_replay_finished leaves the run paused (draft=1, not deleted).

        Replay-derived runs that complete are left paused so the user can finalize
        or discard them. recover_paused_capture_run excludes replay_ IDs, so the
        draft won't silently destroy a real paused reference run on dashboard restart.
        """
        await controller.start_replay(Mode.IDLE, "g1", "/tmp/foo.spinrec")
        run_id = controller.recorder.capture_run_id

        controller.handle_replay_finished()

        row = db.conn.execute(
            "SELECT id, status FROM capture_runs WHERE id = ?", (run_id,)
        ).fetchone()
        assert row is not None, "replay capture_run should remain in DB as paused"
        assert row[1] == "draft", "replay capture_run should still be a draft"
        assert controller.paused_run_id == run_id

    async def test_recovery_non_clobber_after_replay(self, controller, db, tmp_path):
        """After replay, recover_paused_capture_run returns the real paused run.

        The replay run is left as draft=1, but its 'replay_' ID prefix causes
        recover_paused_capture_run to skip it entirely. The real live_ paused run
        is returned instead and is never deleted by the recovery logic.

        Note: start_replay guards against a live paused_run_id, so we simulate the
        crash/restart scenario by writing the real run directly to the DB.
        """
        # Simulate a real paused run already in the DB (e.g. from a previous session)
        real_run_id = "live_real_run"
        db.create_capture_run(real_run_id, "g1", "My Real Run", kind="live")

        # Do a replay (paused_run_id is None so no guard fires)
        await controller.start_replay(Mode.IDLE, "g1", "/tmp/foo.spinrec")
        replay_run_id = controller.recorder.capture_run_id
        assert replay_run_id.startswith("replay_")
        assert replay_run_id != real_run_id

        controller.handle_replay_finished()

        # Replay run is still in DB as paused
        assert db.conn.execute(
            "SELECT id FROM capture_runs WHERE id = ?", (replay_run_id,)
        ).fetchone() is not None
        # Recovery must return the real live_ run, not the replay_ run
        recovered = db.recover_paused_capture_run("g1")
        assert recovered == real_run_id


class TestHandleReplayError:
    async def test_no_segments_deletes_run(self, controller, db):
        """Replay error with no segments captured: hard-delete the empty run."""
        await controller.start_replay(Mode.IDLE, "g1", "/tmp/foo.spinrec")
        run_id = controller.recorder.capture_run_id
        controller.handle_replay_error()
        row = db.conn.execute(
            "SELECT id FROM capture_runs WHERE id = ?", (run_id,)
        ).fetchone()
        assert row is None

    async def test_keeps_run_when_segments_captured(self, controller, db):
        """Replay error after capturing segments: leave as paused so user can decide.

        A replay that partially succeeds (some segments before the error) is
        preserved as a paused run. The user can finalize the partial result
        or discard it. recover_paused_capture_run skips replay_ IDs, so this
        won't interfere with real paused reference runs.
        """
        await controller.start_replay(Mode.IDLE, "g1", "/tmp/foo.spinrec")
        run_id = controller.recorder.capture_run_id
        # Simulate a segment row being captured under this run
        seg = Segment(
            id="seg_replay_err",
            game_id="g1",
            level_number=1,
            start_type=EndpointType.ENTRANCE,
            start_ordinal=0,
            end_type=EndpointType.GOAL,
            end_ordinal=0,
            capture_run_id=run_id,
        )
        db.upsert_segment(seg)

        controller.handle_replay_error()

        row = db.conn.execute(
            "SELECT id, status FROM capture_runs WHERE id = ?", (run_id,)
        ).fetchone()
        assert row is not None, "capture_run should remain when segments were captured"
        assert row[1] == "draft", "capture_run should still be a draft"
        assert controller.paused_run_id == run_id


class TestHandleReplayStarted:
    def test_handle_replay_started_sets_replay_total(self, controller):
        """ReferenceController.handle_replay_started records the frame count
        from the event onto replay_total. handle_replay_finished and
        handle_replay_error reset it back to 0."""
        from spinlab.protocol import ReplayStartedEvent

        assert controller.replay_total == 0

        controller.handle_replay_started(ReplayStartedEvent(path="x.replay", frame_count=2273))
        assert controller.replay_total == 2273

        controller.handle_replay_finished()
        assert controller.replay_total == 0

    async def test_handle_replay_error_resets_replay_total(self, controller):
        """handle_replay_error resets replay_total back to 0."""
        from spinlab.protocol import ReplayStartedEvent

        controller.handle_replay_started(ReplayStartedEvent(path="x.replay", frame_count=500))
        assert controller.replay_total == 500

        await controller.start_replay(Mode.IDLE, "g1", "/tmp/foo.spinrec")
        controller.handle_replay_error()
        assert controller.replay_total == 0


class TestHandleDisconnect:
    async def test_no_segments_pauses_run(self, controller, db, tmp_path):
        await controller.start_reference(Mode.IDLE, "g1", tmp_path)
        run_id = controller.recorder.capture_run_id
        controller.handle_disconnect()
        # Disconnect ends the session but does NOT delete the run — it stays paused.
        assert controller.paused_run_id == run_id
        row = db.conn.execute(
            "SELECT id FROM capture_runs WHERE id = ?", (run_id,)
        ).fetchone()
        assert row is not None, "run should remain in DB as paused, not be deleted"
        sessions = db.list_capture_sessions_for_run(run_id)
        assert sessions[0]["end_reason"] == "disconnected"

    def test_idempotent_when_nothing_active(self, controller):
        controller.handle_disconnect()


class TestRunStateInvariant:
    """`paused_run_id` and `recorder.capture_run_id` must never both be set.

    These tests exercise the assertion directly and then verify each public
    transition (start, stop, resume, finalize, discard, recover) leaves the
    controller in a self-consistent state.
    """

    def test_invariant_raises_when_both_set(self, controller):
        # Bypass the safe transitions to force the bad state.
        controller.paused_run_id = "p"
        controller.recorder.capture_run_id = "r"
        with pytest.raises(AssertionError, match="invariant violated"):
            controller._assert_run_state_invariant()

    def test_invariant_holds_when_idle(self, controller):
        controller._assert_run_state_invariant()
        assert controller.active_run_id is None
        assert not controller.is_recording
        assert not controller.has_paused_run

    async def test_invariant_holds_through_start_stop_resume_finalize(
        self, controller, tmp_path, db,
    ):
        # IDLE → RECORDING
        await controller.start_reference(Mode.IDLE, "g1", tmp_path, run_name="t")
        controller._assert_run_state_invariant()
        assert controller.is_recording
        assert not controller.has_paused_run
        run_id = controller.active_run_id
        assert run_id is not None

        # RECORDING → PAUSED (via stop)
        await controller.stop_reference(Mode.REFERENCE)
        controller._assert_run_state_invariant()
        assert not controller.is_recording
        assert controller.has_paused_run
        assert controller.active_run_id == run_id

        # PAUSED → RECORDING (via resume)
        await controller.resume_reference(Mode.IDLE, "g1", tmp_path)
        controller._assert_run_state_invariant()
        assert controller.is_recording
        assert not controller.has_paused_run
        assert controller.active_run_id == run_id

        # RECORDING → PAUSED (via stop again)
        await controller.stop_reference(Mode.REFERENCE)
        controller._assert_run_state_invariant()

        # PAUSED → IDLE (via finalize)
        await controller.finalize_run("My Run")
        controller._assert_run_state_invariant()
        assert controller.active_run_id is None
        assert not controller.is_recording
        assert not controller.has_paused_run

    async def test_discard_lands_idle(self, controller, tmp_path):
        await controller.start_reference(Mode.IDLE, "g1", tmp_path)
        await controller.stop_reference(Mode.REFERENCE)
        await controller.discard_run()
        controller._assert_run_state_invariant()
        assert controller.active_run_id is None

    async def test_recover_paused_run_enters_paused(self, controller, db):
        # Seed a draft capture_run directly in the DB to simulate restart.
        db.create_capture_run("live_xyz", "g1", "Old draft", kind="live")
        controller.recover_paused_run("g1")
        controller._assert_run_state_invariant()
        assert controller.has_paused_run
        assert controller.active_run_id == "live_xyz"
        assert not controller.is_recording

    async def test_clear_and_idle_resets_from_recording(self, controller, tmp_path):
        await controller.start_reference(Mode.IDLE, "g1", tmp_path)
        controller.clear_and_idle()
        controller._assert_run_state_invariant()
        assert controller.active_run_id is None


class TestSaveAndFinishAtomicity:
    """save_and_finish_run must be all-or-nothing, INCLUDING the session-end.

    Previous behavior: _end_current_session committed `ended_at` outside the
    atomic block, so a finalize that later raised left the session marked
    ended even though the run was still in draft. The fix folds the session-
    end into the same transaction. This test forces a rollback partway and
    verifies session.ended_at is still NULL afterward.
    """

    async def test_failure_inside_atomic_block_rolls_back_session_end(
        self, controller, tmp_path, db, monkeypatch,
    ):
        await controller.start_reference(Mode.IDLE, "g1", tmp_path)
        sess_id = controller.recorder.current_capture_session_id
        run_id = controller.recorder.capture_run_id
        assert sess_id is not None and run_id is not None

        # sqlite3.Connection.execute is C-level read-only, so we wrap the
        # whole conn in a proxy that intercepts a specific SQL pattern and
        # raises mid-transaction.
        real_conn = db.conn

        class FailingConn:
            def execute(self, sql, *args, **kwargs):
                if "UPDATE capture_runs SET status = 'saved'" in sql:
                    raise RuntimeError("simulated finalize failure")
                return real_conn.execute(sql, *args, **kwargs)

            def __getattr__(self, name):
                return getattr(real_conn, name)

        monkeypatch.setattr(db, "conn", FailingConn())

        with pytest.raises(RuntimeError, match="simulated finalize failure"):
            await controller.save_and_finish_run(Mode.REFERENCE, "MyRun")

        monkeypatch.undo()  # restore normal db.conn for assertions

        # Session must NOT be marked ended — that's the audit #11 invariant.
        sess_row = db.conn.execute(
            "SELECT ended_at FROM capture_sessions WHERE id = ?", (sess_id,),
        ).fetchone()
        assert sess_row is not None and sess_row[0] is None

        # Run still in draft, name unchanged.
        run_row = db.conn.execute(
            "SELECT status, name FROM capture_runs WHERE id = ?", (run_id,),
        ).fetchone()
        assert run_row is not None
        assert run_row[0] == "draft"
        assert run_row[1] != "MyRun"

        # Recorder still pointing at the live session — user can retry.
        assert controller.recorder.capture_run_id == run_id
        assert controller.recorder.current_capture_session_id == sess_id


class TestSaveOnEvent:
    """ReferenceController triggers backend save_state on entrance/checkpoint
    while recording. Replaces the orchestrator's _maybe_save_state_for hook."""

    async def test_handle_entrance_saves_state_when_recording(
        self, controller, fake_emu,
    ):
        from spinlab.protocol import LevelEntranceEvent
        controller._enter_recording("run_x", "sess_x")
        await controller.handle_entrance(LevelEntranceEvent(level=5, room=2))
        assert fake_emu.save_state_calls == ["entrance_5_2"]

    async def test_handle_entrance_does_not_save_when_idle(
        self, controller, fake_emu,
    ):
        from spinlab.protocol import LevelEntranceEvent
        await controller.handle_entrance(LevelEntranceEvent(level=5, room=2))
        assert fake_emu.save_state_calls == []

    async def test_handle_checkpoint_saves_hot_state_when_recording(
        self, controller, fake_emu,
    ):
        from spinlab.protocol import CheckpointEvent
        controller._enter_recording("run_x", "sess_x")
        await controller.handle_checkpoint(
            CheckpointEvent(level_num=5, cp_ordinal=1), "g1",
        )
        assert fake_emu.save_state_calls == ["cp_5_1_hot"]

    async def test_handle_checkpoint_does_not_save_when_idle(
        self, controller, fake_emu,
    ):
        from spinlab.protocol import CheckpointEvent
        await controller.handle_checkpoint(
            CheckpointEvent(level_num=5, cp_ordinal=1), "g1",
        )
        assert fake_emu.save_state_calls == []
