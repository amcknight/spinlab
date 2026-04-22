"""Tests for ColdFillController cold-fill flow.

Uses a real Database so the controller's raw SQL queries
(start_waypoint_id lookup, segments_missing_cold) hit real rows
instead of mock cursor chains.
"""
from unittest.mock import AsyncMock, MagicMock

from spinlab.protocol import ColdFillLoadCmd, SpawnEvent

import pytest

from spinlab.capture import ColdFillController
from spinlab.db import Database
from spinlab.errors import NotConnectedError
from spinlab.models import Mode, Segment, Status, Waypoint, WaypointSaveState


@pytest.fixture
def tcp():
    tcp = MagicMock()
    tcp.is_connected = True
    tcp.send = AsyncMock()
    tcp.send_command = AsyncMock()
    return tcp


@pytest.fixture
def cold_fill_db(tmp_path):
    """Real DB with 2 checkpoint segments that have hot but no cold save states.

    segments_missing_cold("g") will return both segments.
    """
    db = Database(tmp_path / "cold_fill.db")
    db.upsert_game("g", "Game", "any%")

    # Create waypoints for 2 checkpoint segments within level 105
    wp_entrance = Waypoint.make("g", 105, "entrance", 0, {})
    wp_cp1 = Waypoint.make("g", 105, "checkpoint", 1, {})
    wp_cp2 = Waypoint.make("g", 105, "checkpoint", 2, {})
    wp_goal = Waypoint.make("g", 105, "goal", 0, {})
    for wp in (wp_entrance, wp_cp1, wp_cp2, wp_goal):
        db.upsert_waypoint(wp)

    # Segment 1: cp1 → cp2
    seg1_id = Segment.make_id("g", 105, "checkpoint", 1, "checkpoint", 2,
                              wp_cp1.id, wp_cp2.id)
    db.upsert_segment(Segment(
        id=seg1_id, game_id="g", level_number=105,
        start_type="checkpoint", start_ordinal=1,
        end_type="checkpoint", end_ordinal=2,
        description="", ordinal=1,
        start_waypoint_id=wp_cp1.id, end_waypoint_id=wp_cp2.id,
    ))

    # Segment 2: cp2 → goal
    seg2_id = Segment.make_id("g", 105, "checkpoint", 2, "goal", 0,
                              wp_cp2.id, wp_goal.id)
    db.upsert_segment(Segment(
        id=seg2_id, game_id="g", level_number=105,
        start_type="checkpoint", start_ordinal=2,
        end_type="goal", end_ordinal=0,
        description="", ordinal=2,
        start_waypoint_id=wp_cp2.id, end_waypoint_id=wp_goal.id,
    ))

    # Hot save states for each start waypoint (no cold → segments_missing_cold returns them)
    hot1 = tmp_path / "hot1.mss"
    hot1.write_bytes(b"fake hot 1")
    db.add_save_state(WaypointSaveState(
        waypoint_id=wp_cp1.id, variant_type="hot",
        state_path=str(hot1), is_default=True,
    ))

    hot2 = tmp_path / "hot2.mss"
    hot2.write_bytes(b"fake hot 2")
    db.add_save_state(WaypointSaveState(
        waypoint_id=wp_cp2.id, variant_type="hot",
        state_path=str(hot2), is_default=True,
    ))

    db._seg1_id = seg1_id
    db._seg2_id = seg2_id
    db._wp_cp1_id = wp_cp1.id
    db._wp_cp2_id = wp_cp2.id
    db._hot1_path = str(hot1)
    db._hot2_path = str(hot2)
    return db


class TestStartColdFill:
    async def test_start_cold_fill_sends_first_segment(self, tcp, cold_fill_db):
        cc = ColdFillController(cold_fill_db, tcp)
        result = await cc.start("g")

        assert result.status == Status.STARTED
        assert result.new_mode == Mode.COLD_FILL

        # Verify Lua command sent for first segment
        cmd = tcp.send_command.call_args[0][0]
        assert isinstance(cmd, ColdFillLoadCmd)
        assert cmd.event == "cold_fill_load"
        assert cmd.state_path == cold_fill_db._hot1_path
        assert cmd.segment_id == cold_fill_db._seg1_id

    async def test_start_cold_fill_no_gaps(self, tcp, cold_fill_db):
        # Add cold save states so there are no gaps
        cold_fill_db.add_save_state(WaypointSaveState(
            waypoint_id=cold_fill_db._wp_cp1_id, variant_type="cold",
            state_path="/cold1.mss", is_default=True,
        ))
        cold_fill_db.add_save_state(WaypointSaveState(
            waypoint_id=cold_fill_db._wp_cp2_id, variant_type="cold",
            state_path="/cold2.mss", is_default=True,
        ))
        cc = ColdFillController(cold_fill_db, tcp)
        result = await cc.start("g")
        assert result.status == Status.NO_GAPS

    async def test_start_cold_fill_not_connected(self, tcp, cold_fill_db):
        tcp.is_connected = False
        cc = ColdFillController(cold_fill_db, tcp)
        with pytest.raises(NotConnectedError):
            await cc.start("g")


class TestHandleColdFillSpawn:
    async def test_stores_cold_save_state_and_advances(self, tcp, cold_fill_db):
        cc = ColdFillController(cold_fill_db, tcp)
        await cc.start("g")

        # Simulate spawn event for first segment
        done = await cc.handle_spawn(
            SpawnEvent(state_captured=True, state_path="/cold1.mss"),
        )
        assert done is False  # still have one more

        # Verify cold save state stored in DB
        cold = cold_fill_db.get_save_state(cold_fill_db._wp_cp1_id, "cold")
        assert cold is not None
        assert cold.variant_type == "cold"
        assert cold.state_path == "/cold1.mss"
        assert cold.is_default is True

        # Verify second segment loaded
        cmd = tcp.send_command.call_args[0][0]
        assert isinstance(cmd, ColdFillLoadCmd)
        assert cmd.event == "cold_fill_load"
        assert cmd.segment_id == cold_fill_db._seg2_id

    async def test_returns_true_when_queue_empty(self, tcp, cold_fill_db):
        cc = ColdFillController(cold_fill_db, tcp)
        await cc.start("g")

        # Process both segments
        await cc.handle_spawn(
            SpawnEvent(state_captured=True, state_path="/cold1.mss"),
        )
        done = await cc.handle_spawn(
            SpawnEvent(state_captured=True, state_path="/cold2.mss"),
        )
        assert done is True

    async def test_ignores_spawn_without_state(self, tcp, cold_fill_db):
        cc = ColdFillController(cold_fill_db, tcp)
        await cc.start("g")

        done = await cc.handle_spawn(
            SpawnEvent(state_captured=False),
        )
        assert done is False
        # Queue unchanged — still on first segment
        assert cc.current == cold_fill_db._seg1_id


class TestGetColdFillState:
    async def test_returns_none_before_start(self, tcp, cold_fill_db):
        cc = ColdFillController(cold_fill_db, tcp)
        assert cc.get_state() is None

    async def test_returns_progress_mid_fill(self, tcp, cold_fill_db):
        cc = ColdFillController(cold_fill_db, tcp)
        await cc.start("g")

        state = cc.get_state()
        assert state["current"] == 1
        assert state["total"] == 2
        assert state["segment_label"] == "L105 cp1 > cp2"

    async def test_progress_advances(self, tcp, cold_fill_db):
        cc = ColdFillController(cold_fill_db, tcp)
        await cc.start("g")

        await cc.handle_spawn(
            SpawnEvent(state_captured=True, state_path="/cold1.mss"),
        )
        state = cc.get_state()
        assert state["current"] == 2
        assert state["total"] == 2
        assert state["segment_label"] == "L105 cp2 > goal"

    async def test_returns_none_after_complete(self, tcp, cold_fill_db):
        cc = ColdFillController(cold_fill_db, tcp)
        await cc.start("g")
        await cc.handle_spawn(
            SpawnEvent(state_captured=True, state_path="/cold1.mss"),
        )
        await cc.handle_spawn(
            SpawnEvent(state_captured=True, state_path="/cold2.mss"),
        )
        assert cc.get_state() is None

    async def test_uses_description_when_present(self, tcp, cold_fill_db):
        # Update segment description in DB
        cold_fill_db.update_segment(cold_fill_db._seg1_id, description="My Custom Name")

        cc = ColdFillController(cold_fill_db, tcp)
        await cc.start("g")
        state = cc.get_state()
        assert state["segment_label"] == "My Custom Name"
