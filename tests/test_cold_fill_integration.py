"""Integration test: full cold-fill cycle with real DB."""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from spinlab.db import Database
from spinlab.models import Mode, Segment, Waypoint, WaypointSaveState, Status
from spinlab.session_manager import SessionManager


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "test.db"))
    d.upsert_game("g1", "Test Game", "any%")
    return d


@pytest.fixture
def tcp():
    tcp = MagicMock()
    tcp.is_connected = True
    tcp.send = AsyncMock()
    return tcp


@pytest.fixture
def sm(db, tcp):
    return SessionManager(db=db, tcp=tcp, rom_dir=None)


def _create_segments_with_hot_only(db):
    """Create 3 segments with waypoints: entrance>cp1, cp1>cp2, cp2>goal.
    Entrance waypoint gets cold (entrance IS the cold start).
    cp1 and cp2 waypoints get only hot.
    """
    game_id = "g1"
    level = 105

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
            reference_id="run1",
        ),
        Segment(
            id=Segment.make_id(game_id, level, "checkpoint", 1, "checkpoint", 2,
                               wp_cp1.id, wp_cp2.id),
            game_id=game_id, level_number=level,
            start_type="checkpoint", start_ordinal=1,
            end_type="checkpoint", end_ordinal=2,
            start_waypoint_id=wp_cp1.id, end_waypoint_id=wp_cp2.id,
            reference_id="run1",
        ),
        Segment(
            id=Segment.make_id(game_id, level, "checkpoint", 2, "goal", 0,
                               wp_cp2.id, wp_goal.id),
            game_id=game_id, level_number=level,
            start_type="checkpoint", start_ordinal=2,
            end_type="goal", end_ordinal=0,
            start_waypoint_id=wp_cp2.id, end_waypoint_id=wp_goal.id,
            reference_id="run1",
        ),
    ]
    for s in segs:
        db.upsert_segment(s)

    # Entrance segment: cold save state (entrance IS the cold start)
    db.add_save_state(WaypointSaveState(wp_entrance.id, "cold", "/cold0.mss", True))
    # cp1 and cp2: hot save states only (cold fill will capture cold ones)
    db.add_save_state(WaypointSaveState(wp_cp1.id, "hot", "/hot1.mss", True))
    db.add_save_state(WaypointSaveState(wp_cp2.id, "hot", "/hot2.mss", True))

    return segs, wp_cp1, wp_cp2


class TestColdFillIntegration:
    async def test_full_cycle(self, sm, db, tcp):
        sm.game_id = "g1"

        # Set up and save draft — capture run must exist before segments (FK)
        db.create_capture_run("run1", "g1", "Test Run", draft=True)
        segs, wp_cp1, wp_cp2 = _create_segments_with_hot_only(db)
        sm.capture.draft.enter_draft("run1", 3)
        result = await sm.save_draft("Test Run")

        assert result.status == Status.OK
        assert sm.mode == Mode.COLD_FILL

        # Verify first cold-gap segment loaded (cp1 has hot but not cold)
        sent = json.loads(tcp.send.call_args[0][0])
        assert sent["event"] == "cold_fill_load"
        assert sent["state_path"] == "/hot1.mss"
        assert sent["segment_id"] == segs[1].id

        # Simulate spawn for first segment
        await sm.route_event({
            "event": "spawn",
            "state_captured": True,
            "state_path": "/cold1.mss",
        })
        assert sm.mode == Mode.COLD_FILL  # still filling

        # Verify cold save state stored on cp1 waypoint
        ss = db.get_save_state(wp_cp1.id, "cold")
        assert ss is not None
        assert ss.state_path == "/cold1.mss"
        assert ss.is_default is True

        # Simulate spawn for second segment
        await sm.route_event({
            "event": "spawn",
            "state_captured": True,
            "state_path": "/cold2.mss",
        })
        assert sm.mode == Mode.IDLE  # done

        # Verify cold save state stored on cp2 waypoint
        ss2 = db.get_save_state(wp_cp2.id, "cold")
        assert ss2 is not None
        assert ss2.state_path == "/cold2.mss"

        # Verify no more gaps
        assert db.segments_missing_cold("g1") == []
