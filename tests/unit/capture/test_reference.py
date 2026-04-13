"""Tests for ReferenceController orchestration logic.

Uses a real SQLite Database (tmp_path) and FakeTcpManager to exercise the
controller's real interactions with the DB schema and TCP protocol.
Mocking both collaborators would reduce these to tautology tests.
"""
import pytest

from spinlab.capture import ReferenceController
from spinlab.db import Database
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
    async def test_guard_draft_pending(self, controller, tmp_path):
        controller.draft.run_id = "fake_draft_run"
        result = await controller.start_reference(Mode.IDLE, "g1", tmp_path, run_name="test")
        assert result.status == Status.DRAFT_PENDING

    async def test_guard_practice_active(self, controller, tmp_path):
        result = await controller.start_reference(Mode.PRACTICE, "g1", tmp_path)
        assert result.status == Status.PRACTICE_ACTIVE

    async def test_guard_already_replaying(self, controller, tmp_path):
        result = await controller.start_reference(Mode.REPLAY, "g1", tmp_path)
        assert result.status == Status.ALREADY_REPLAYING

    async def test_guard_not_connected(self, controller, tmp_path, fake_tcp):
        fake_tcp.is_connected = False
        result = await controller.start_reference(Mode.IDLE, "g1", tmp_path)
        assert result.status == Status.NOT_CONNECTED

    async def test_happy_path(self, controller, tmp_path, fake_tcp):
        result = await controller.start_reference(Mode.IDLE, "g1", tmp_path, run_name="my run")
        assert result.status == Status.STARTED
        assert result.new_mode == Mode.REFERENCE
        assert len(fake_tcp.sent_commands) == 1
        assert isinstance(fake_tcp.sent_commands[0], ReferenceStartCmd)
        assert controller.recorder.capture_run_id is not None


class TestStopReference:
    async def test_not_in_reference(self, controller):
        result = await controller.stop_reference(Mode.IDLE)
        assert result.status == Status.NOT_IN_REFERENCE

    async def test_happy_path_enters_draft(self, controller, tmp_path, fake_tcp):
        await controller.start_reference(Mode.IDLE, "g1", tmp_path)
        controller.recorder.segment_times = []

        result = await controller.stop_reference(Mode.REFERENCE)

        assert result.status == Status.STOPPED
        assert result.new_mode == Mode.IDLE
        stop_cmds = [c for c in fake_tcp.sent_commands if isinstance(c, ReferenceStopCmd)]
        assert len(stop_cmds) == 1


class TestStartReplay:
    async def test_guard_reference_active(self, controller):
        result = await controller.start_replay(Mode.REFERENCE, "g1", "/tmp/foo.spinrec")
        assert result.status == Status.REFERENCE_ACTIVE

    async def test_guard_already_replaying(self, controller):
        result = await controller.start_replay(Mode.REPLAY, "g1", "/tmp/foo.spinrec")
        assert result.status == Status.ALREADY_REPLAYING

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
        result = await controller.stop_replay(Mode.IDLE)
        assert result.status == Status.NOT_REPLAYING

    async def test_no_segments_hard_deletes_run(self, controller, db, fake_tcp):
        await controller.start_replay(Mode.IDLE, "g1", "/tmp/foo.spinrec")
        run_id = controller.recorder.capture_run_id

        result = await controller.stop_replay(Mode.REPLAY)

        assert result.status == Status.STOPPED
        row = db.conn.execute(
            "SELECT id FROM capture_runs WHERE id = ?", (run_id,)
        ).fetchone()
        assert row is None, "capture_run should have been hard-deleted"


class TestHandleReplayError:
    async def test_no_segments_deletes_run(self, controller, db):
        await controller.start_replay(Mode.IDLE, "g1", "/tmp/foo.spinrec")
        run_id = controller.recorder.capture_run_id
        controller.handle_replay_error()
        row = db.conn.execute(
            "SELECT id FROM capture_runs WHERE id = ?", (run_id,)
        ).fetchone()
        assert row is None


class TestHandleDisconnect:
    async def test_no_segments_deletes_run(self, controller, db, tmp_path):
        await controller.start_reference(Mode.IDLE, "g1", tmp_path)
        run_id = controller.recorder.capture_run_id
        controller.handle_disconnect()
        row = db.conn.execute(
            "SELECT id FROM capture_runs WHERE id = ?", (run_id,)
        ).fetchone()
        assert row is None

    def test_idempotent_when_nothing_active(self, controller):
        controller.handle_disconnect()


class TestStartFillGap:
    async def test_not_connected(self, controller, fake_tcp):
        fake_tcp.is_connected = False
        result = await controller.start_fill_gap("seg1")
        assert result.status == Status.NOT_CONNECTED

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

        result = await controller.start_fill_gap("seg1")
        assert result.status == Status.NO_HOT_VARIANT

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
