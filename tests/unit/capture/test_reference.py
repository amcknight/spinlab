"""Tests for ReferenceController orchestration logic.

Uses a real SQLite Database (tmp_path) and FakeTcpManager to exercise the
controller's real interactions with the DB schema and TCP protocol.
Mocking both collaborators would reduce these to tautology tests.
"""
import pytest

from spinlab.capture import ReferenceController
from spinlab.db import Database
from spinlab.errors import (
    AlreadyReplayingError,
    NoHotVariantError,
    NotConnectedError,
    NotInReferenceError,
    NotReplayingError,
    PracticeActiveError,
    ReferenceActiveError,
    RunPendingError,
)
from spinlab.models import EndpointType, Mode, Segment, Status, Waypoint, WaypointSaveState
from spinlab.protocol import (
    FillGapLoadCmd,
    ReferenceStartCmd,
    ReferenceStopCmd,
    ReplayCmd,
    ReplayStopCmd,
)


@pytest.fixture
def db(tmp_path):
    """Real SQLite database, per-test."""
    d = Database(tmp_path / "test.db")
    d.upsert_game("g1", "Test Game", "any%")
    return d


@pytest.fixture
def controller(db, fake_tcp):
    return ReferenceController(db, fake_tcp)


class TestStartReference:
    async def test_guard_paused_run_pending(self, controller, tmp_path):
        controller.paused_run_id = "fake_paused_run"
        with pytest.raises(RunPendingError):
            await controller.start_reference(Mode.IDLE, "g1", tmp_path, run_name="test")

    async def test_guard_practice_active(self, controller, tmp_path):
        with pytest.raises(PracticeActiveError):
            await controller.start_reference(Mode.PRACTICE, "g1", tmp_path)

    async def test_guard_already_replaying(self, controller, tmp_path):
        with pytest.raises(AlreadyReplayingError):
            await controller.start_reference(Mode.REPLAY, "g1", tmp_path)

    async def test_guard_not_connected(self, controller, tmp_path, fake_tcp):
        fake_tcp.is_connected = False
        with pytest.raises(NotConnectedError):
            await controller.start_reference(Mode.IDLE, "g1", tmp_path)

    async def test_happy_path(self, controller, tmp_path, fake_tcp):
        result = await controller.start_reference(Mode.IDLE, "g1", tmp_path, run_name="my run")
        assert result.status == Status.STARTED
        assert result.new_mode == Mode.REFERENCE
        assert len(fake_tcp.sent_commands) == 1
        assert isinstance(fake_tcp.sent_commands[0], ReferenceStartCmd)
        assert controller.recorder.capture_run_id is not None


class TestStopReference:
    async def test_not_in_reference(self, controller):
        with pytest.raises(NotInReferenceError):
            await controller.stop_reference(Mode.IDLE)

    async def test_happy_path_pauses_run(self, controller, tmp_path, fake_tcp, db):
        await controller.start_reference(Mode.IDLE, "g1", tmp_path)
        run_id = controller.recorder.capture_run_id

        result = await controller.stop_reference(Mode.REFERENCE)

        assert result.status == Status.STOPPED
        assert result.new_mode == Mode.IDLE
        stop_cmds = [c for c in fake_tcp.sent_commands if isinstance(c, ReferenceStopCmd)]
        assert len(stop_cmds) == 1
        assert controller.paused_run_id == run_id
        row = db.conn.execute(
            "SELECT draft FROM capture_runs WHERE id = ?", (run_id,)
        ).fetchone()
        assert row is not None and row[0] == 1


class TestStartReplay:
    async def test_guard_reference_active(self, controller):
        with pytest.raises(ReferenceActiveError):
            await controller.start_replay(Mode.REFERENCE, "g1", "/tmp/foo.spinrec")

    async def test_guard_already_replaying(self, controller):
        with pytest.raises(AlreadyReplayingError):
            await controller.start_replay(Mode.REPLAY, "g1", "/tmp/foo.spinrec")

    async def test_happy_path(self, controller, fake_tcp):
        result = await controller.start_replay(Mode.IDLE, "g1", "/tmp/foo.spinrec", speed=2)
        assert result.status == Status.STARTED
        assert result.new_mode == Mode.REPLAY
        replay_cmds = [c for c in fake_tcp.sent_commands if isinstance(c, ReplayCmd)]
        assert len(replay_cmds) == 1
        assert replay_cmds[0].path == "/tmp/foo.spinrec"
        assert replay_cmds[0].speed == 2


class TestStopReplay:
    async def test_not_replaying(self, controller):
        with pytest.raises(NotReplayingError):
            await controller.stop_replay(Mode.IDLE)

    async def test_no_segments_hard_deletes_run(self, controller, db, fake_tcp):
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
            "SELECT id, draft FROM capture_runs WHERE id = ?", (run_id,)
        ).fetchone()
        assert row is not None, "replay capture_run should remain in DB as paused"
        assert row[1] == 1, "replay capture_run should still be draft=1"
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
        db.create_capture_run(real_run_id, "g1", "My Real Run", draft=True)

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
            reference_id=run_id,
        )
        db.upsert_segment(seg)

        controller.handle_replay_error()

        row = db.conn.execute(
            "SELECT id, draft FROM capture_runs WHERE id = ?", (run_id,)
        ).fetchone()
        assert row is not None, "capture_run should remain when segments were captured"
        assert row[1] == 1, "capture_run should still be draft=1"
        assert controller.paused_run_id == run_id


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


class TestStartFillGap:
    async def test_not_connected(self, controller, fake_tcp):
        fake_tcp.is_connected = False
        with pytest.raises(NotConnectedError):
            await controller.start_fill_gap("seg1")

    async def test_no_hot_variant(self, controller, db):
        wp_start = Waypoint.make("g1", 1, "entrance", 0, {})
        wp_end = Waypoint.make("g1", 1, "goal", 0, {})
        db.upsert_waypoint(wp_start)
        db.upsert_waypoint(wp_end)
        seg = Segment(
            id="seg1", game_id="g1", level_number=1,
            start_type=EndpointType.ENTRANCE, start_ordinal=0,
            end_type=EndpointType.GOAL, end_ordinal=0,
            start_waypoint_id=wp_start.id, end_waypoint_id=wp_end.id,
        )
        db.upsert_segment(seg)

        with pytest.raises(NoHotVariantError):
            await controller.start_fill_gap("seg1")

    async def test_happy_path(self, controller, db, tmp_path, fake_tcp):
        wp_start = Waypoint.make("g1", 1, "entrance", 0, {})
        wp_end = Waypoint.make("g1", 1, "goal", 0, {})
        db.upsert_waypoint(wp_start)
        db.upsert_waypoint(wp_end)
        seg = Segment(
            id="seg1", game_id="g1", level_number=1,
            start_type=EndpointType.ENTRANCE, start_ordinal=0,
            end_type=EndpointType.GOAL, end_ordinal=0,
            start_waypoint_id=wp_start.id, end_waypoint_id=wp_end.id,
        )
        db.upsert_segment(seg)
        state_file = tmp_path / "hot.mss"
        state_file.write_bytes(b"fake")
        db.add_save_state(WaypointSaveState(
            waypoint_id=wp_start.id, variant_type="hot",
            state_path=str(state_file), is_default=True,
        ))

        result = await controller.start_fill_gap("seg1")
        assert result.status == Status.STARTED
        assert result.new_mode == Mode.FILL_GAP
        fill_cmds = [c for c in fake_tcp.sent_commands if isinstance(c, FillGapLoadCmd)]
        assert len(fill_cmds) == 1
        assert fill_cmds[0].state_path == str(state_file)
