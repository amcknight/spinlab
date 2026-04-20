"""Tests for ColdFillController cold-fill flow."""
from unittest.mock import AsyncMock, MagicMock, call

from spinlab.protocol import ColdFillLoadCmd

import pytest

from spinlab.capture import ColdFillController
from spinlab.errors import NotConnectedError
from spinlab.models import Mode, Status, WaypointSaveState


def _make_conn_mock(waypoint_id: str = "wp_start_1"):
    """Return a mock db.conn that returns a start_waypoint_id for any segment lookup."""
    conn = MagicMock()
    row = MagicMock()
    row.__getitem__ = MagicMock(side_effect=lambda k: waypoint_id if k == 0 else None)
    conn.execute.return_value.fetchone.return_value = row
    return conn


@pytest.fixture
def tcp():
    tcp = MagicMock()
    tcp.is_connected = True
    tcp.send = AsyncMock()
    tcp.send_command = AsyncMock()
    return tcp


@pytest.fixture
def db():
    db = MagicMock()
    db.segments_missing_cold = MagicMock(return_value=[
        {"segment_id": "g1:105:cp.1:cp.2", "hot_state_path": "/hot1.mss",
         "level_number": 105, "start_type": "checkpoint", "start_ordinal": 1,
         "end_type": "checkpoint", "end_ordinal": 2, "description": ""},
        {"segment_id": "g1:105:cp.2:goal.0", "hot_state_path": "/hot2.mss",
         "level_number": 105, "start_type": "checkpoint", "start_ordinal": 2,
         "end_type": "goal", "end_ordinal": 0, "description": ""},
    ])
    db.add_save_state = MagicMock()
    db.conn = _make_conn_mock("wp_start_1")
    return db


class TestStartColdFill:
    async def test_start_cold_fill_sends_first_segment(self, tcp, db):
        cc = ColdFillController(db, tcp)
        result = await cc.start("g1")

        assert result.status == Status.STARTED
        assert result.new_mode == Mode.COLD_FILL

        # Verify Lua command sent for first segment
        cmd = tcp.send_command.call_args[0][0]
        assert isinstance(cmd, ColdFillLoadCmd)
        assert cmd.event == "cold_fill_load"
        assert cmd.state_path == "/hot1.mss"
        assert cmd.segment_id == "g1:105:cp.1:cp.2"

    async def test_start_cold_fill_no_gaps(self, tcp, db):
        db.segments_missing_cold.return_value = []
        cc = ColdFillController(db, tcp)
        result = await cc.start("g1")
        assert result.status == Status.NO_GAPS

    async def test_start_cold_fill_not_connected(self, tcp, db):
        tcp.is_connected = False
        cc = ColdFillController(db, tcp)
        with pytest.raises(NotConnectedError):
            await cc.start("g1")


class TestHandleColdFillSpawn:
    async def test_stores_cold_save_state_and_advances(self, tcp, db):
        cc = ColdFillController(db, tcp)
        await cc.start("g1")

        # Simulate spawn event for first segment
        done = await cc.handle_spawn(
            {"state_captured": True, "state_path": "/cold1.mss"},
        )
        assert done is False  # still have one more

        # Verify cold save state stored via waypoint API
        ss = db.add_save_state.call_args[0][0]
        assert isinstance(ss, WaypointSaveState)
        assert ss.variant_type == "cold"
        assert ss.state_path == "/cold1.mss"
        assert ss.is_default is True

        # Verify second segment loaded
        cmd = tcp.send_command.call_args[0][0]
        assert isinstance(cmd, ColdFillLoadCmd)
        assert cmd.event == "cold_fill_load"
        assert cmd.segment_id == "g1:105:cp.2:goal.0"

    async def test_returns_true_when_queue_empty(self, tcp, db):
        cc = ColdFillController(db, tcp)
        await cc.start("g1")

        # Process both segments
        await cc.handle_spawn(
            {"state_captured": True, "state_path": "/cold1.mss"},
        )
        done = await cc.handle_spawn(
            {"state_captured": True, "state_path": "/cold2.mss"},
        )
        assert done is True

    async def test_ignores_spawn_without_state(self, tcp, db):
        cc = ColdFillController(db, tcp)
        await cc.start("g1")

        done = await cc.handle_spawn(
            {"state_captured": False},
        )
        assert done is False
        # Queue unchanged — still on first segment
        assert cc.current == "g1:105:cp.1:cp.2"


class TestGetColdFillState:
    async def test_returns_none_before_start(self, tcp, db):
        cc = ColdFillController(db, tcp)
        assert cc.get_state() is None

    async def test_returns_progress_mid_fill(self, tcp, db):
        cc = ColdFillController(db, tcp)
        await cc.start("g1")

        state = cc.get_state()
        assert state["current"] == 1
        assert state["total"] == 2
        assert state["segment_label"] == "L105 cp1 > cp2"

    async def test_progress_advances(self, tcp, db):
        cc = ColdFillController(db, tcp)
        await cc.start("g1")

        await cc.handle_spawn(
            {"state_captured": True, "state_path": "/cold1.mss"},
        )
        state = cc.get_state()
        assert state["current"] == 2
        assert state["total"] == 2
        assert state["segment_label"] == "L105 cp2 > goal"

    async def test_returns_none_after_complete(self, tcp, db):
        cc = ColdFillController(db, tcp)
        await cc.start("g1")
        await cc.handle_spawn(
            {"state_captured": True, "state_path": "/cold1.mss"},
        )
        await cc.handle_spawn(
            {"state_captured": True, "state_path": "/cold2.mss"},
        )
        assert cc.get_state() is None

    async def test_uses_description_when_present(self, tcp, db):
        db.segments_missing_cold.return_value = [
            {"segment_id": "seg1", "hot_state_path": "/hot.mss",
             "level_number": 105, "start_type": "checkpoint", "start_ordinal": 1,
             "end_type": "goal", "end_ordinal": 0, "description": "My Custom Name"},
        ]
        cc = ColdFillController(db, tcp)
        await cc.start("g1")
        state = cc.get_state()
        assert state["segment_label"] == "My Custom Name"
