"""Integration test: full cold-fill cycle with real DB."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from spinlab.db import Database
from spinlab.models import Mode, Segment, Status, Waypoint, WaypointSaveState
from spinlab.protocol import ColdFillLoadCmd, SpawnEvent
from spinlab.session_manager import SessionManager


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "test.db"))
    d.upsert_game("g1", "Test Game", "any%")
    return d


@pytest.fixture
def emu():
    emu = MagicMock()
    emu.is_connected = True
    emu.send = AsyncMock()
    emu.send_command = AsyncMock()
    emu.save_state = AsyncMock()
    emu.load_state = AsyncMock()
    return emu


@pytest.fixture
def sm(db, emu):
    return SessionManager(db=db, emu=emu, rom_dir=None)


def _create_segments_with_hot_only(db, tmp_path=None):
    """Create 3 segments with waypoints: entrance>cp1, cp1>cp2, cp2>goal.
    Entrance waypoint gets cold (entrance IS the cold start).
    cp1 and cp2 waypoints get only hot.

    If tmp_path is given, hot/cold files are written there as empty files —
    needed because cold_fill now defensively skips segments with missing
    state files on disk.
    """
    game_id = "g1"
    level = 105

    def _path(name: str) -> str:
        if tmp_path is None:
            return f"/{name}"
        p = tmp_path / name
        p.write_bytes(b"")
        return str(p)

    # Build waypoints for each boundary
    wp_entrance = Waypoint.make(game_id, level, "entrance", 0, {})
    wp_cp1 = Waypoint.make(game_id, level, "checkpoint", 1, {})
    wp_cp2 = Waypoint.make(game_id, level, "checkpoint", 2, {})
    wp_goal = Waypoint.make(game_id, level, "goal", 0, {})
    for wp in [wp_entrance, wp_cp1, wp_cp2, wp_goal]:
        db.upsert_waypoint(wp)

    segs = [
        Segment(
            id=Segment.make_id(game_id, level, "entrance", 0, "checkpoint", 1,
                               wp_entrance.id, wp_cp1.id),
            game_id=game_id, level_number=level,
            start_type="entrance", start_ordinal=0,
            end_type="checkpoint", end_ordinal=1,
            start_waypoint_id=wp_entrance.id, end_waypoint_id=wp_cp1.id,
            capture_run_id="run1",
        ),
        Segment(
            id=Segment.make_id(game_id, level, "checkpoint", 1, "checkpoint", 2,
                               wp_cp1.id, wp_cp2.id),
            game_id=game_id, level_number=level,
            start_type="checkpoint", start_ordinal=1,
            end_type="checkpoint", end_ordinal=2,
            start_waypoint_id=wp_cp1.id, end_waypoint_id=wp_cp2.id,
            capture_run_id="run1",
        ),
        Segment(
            id=Segment.make_id(game_id, level, "checkpoint", 2, "goal", 0,
                               wp_cp2.id, wp_goal.id),
            game_id=game_id, level_number=level,
            start_type="checkpoint", start_ordinal=2,
            end_type="goal", end_ordinal=0,
            start_waypoint_id=wp_cp2.id, end_waypoint_id=wp_goal.id,
            capture_run_id="run1",
        ),
    ]
    for s in segs:
        db.upsert_segment(s)

    # Entrance segment: cold save state (entrance IS the cold start)
    db.add_save_state(WaypointSaveState(wp_entrance.id, "cold", _path("cold0.mss")))
    # cp1 and cp2: hot save states only (cold fill will capture cold ones)
    db.add_save_state(WaypointSaveState(wp_cp1.id, "hot", _path("hot1.mss")))
    db.add_save_state(WaypointSaveState(wp_cp2.id, "hot", _path("hot2.mss")))

    return segs, wp_cp1, wp_cp2


class TestColdFillIntegration:
    async def test_full_cycle(self, sm, db, emu, tmp_path):
        sm.game_id = "g1"
        db.create_capture_run("run1", "g1", "Test Run", kind="live")
        segs, wp_cp1, wp_cp2 = _create_segments_with_hot_only(db, tmp_path=tmp_path)
        sm.capture.paused_run_id = "run1"

        # Finalize no longer auto-enters cold-fill.
        result = await sm.finalize_run("Test Run")
        assert result.status == Status.OK
        assert sm.mode == Mode.IDLE

        # User starts cold-fill for the active run.
        db.set_active_capture_run("run1")
        start = await sm.cold_fill.start("g1", run_id="run1")
        if start.new_mode == Mode.COLD_FILL:
            sm.mode = Mode.COLD_FILL
        assert sm.mode == Mode.COLD_FILL

        cmd = emu.send_command.call_args[0][0]
        assert isinstance(cmd, ColdFillLoadCmd)
        assert cmd.state_path == str(tmp_path / "hot1.mss")
        assert cmd.segment_id == segs[1].id

        await sm.route_event(SpawnEvent(state_path="/cold1.mss"))
        assert sm.mode == Mode.COLD_FILL
        assert db.get_save_state(wp_cp1.id, "cold").state_path == "/cold1.mss"

        await sm.route_event(SpawnEvent(state_path="/cold2.mss"))
        assert sm.mode == Mode.IDLE
        assert db.get_save_state(wp_cp2.id, "cold").state_path == "/cold2.mss"
        assert db.segments_missing_cold("g1", run_id="run1") == []

    async def test_finalize_does_not_auto_enter_cold_fill(self, sm, db, emu, tmp_path):
        sm.game_id = "g1"
        db.create_capture_run("run1", "g1", "Test Run", kind="live")
        _create_segments_with_hot_only(db, tmp_path=tmp_path)
        sm.capture.paused_run_id = "run1"
        emu.send_command.reset_mock()
        await sm.finalize_run("Test Run")
        assert sm.mode == Mode.IDLE
        emu.send_command.assert_not_called()  # no ColdFillLoadCmd fired
