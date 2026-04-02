"""Tests for CaptureController cold-fill flow."""
import json
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from spinlab.capture_controller import CaptureController
from spinlab.models import Mode, SegmentVariant


@pytest.fixture
def tcp():
    tcp = MagicMock()
    tcp.is_connected = True
    tcp.send = AsyncMock()
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
    db.get_variant = MagicMock(return_value=None)
    db.add_variant = MagicMock()
    return db


class TestStartColdFill:
    async def test_start_cold_fill_sends_first_segment(self, tcp, db):
        cc = CaptureController()
        result = await cc.start_cold_fill("g1", tcp, db)

        assert result["status"] == "started"
        assert result["new_mode"] == Mode.COLD_FILL
        assert result["total"] == 2
        assert result["current"] == 1

        # Verify Lua command sent for first segment
        sent = json.loads(tcp.send.call_args[0][0])
        assert sent["event"] == "cold_fill_load"
        assert sent["state_path"] == "/hot1.mss"
        assert sent["segment_id"] == "g1:105:cp.1:cp.2"

    async def test_start_cold_fill_no_gaps(self, tcp, db):
        db.segments_missing_cold.return_value = []
        cc = CaptureController()
        result = await cc.start_cold_fill("g1", tcp, db)
        assert result["status"] == "no_gaps"

    async def test_start_cold_fill_not_connected(self, tcp, db):
        tcp.is_connected = False
        cc = CaptureController()
        result = await cc.start_cold_fill("g1", tcp, db)
        assert result["status"] == "not_connected"


class TestHandleColdFillSpawn:
    async def test_stores_cold_variant_and_advances(self, tcp, db):
        cc = CaptureController()
        await cc.start_cold_fill("g1", tcp, db)

        # Simulate spawn event for first segment
        done = await cc.handle_cold_fill_spawn(
            {"state_captured": True, "state_path": "/cold1.mss"},
            tcp, db,
        )
        assert done is False  # still have one more

        # Verify cold variant stored with is_default=True
        v = db.add_variant.call_args[0][0]
        assert v.segment_id == "g1:105:cp.1:cp.2"
        assert v.variant_type == "cold"
        assert v.state_path == "/cold1.mss"
        assert v.is_default is True

        # Verify second segment loaded
        sent = json.loads(tcp.send.call_args[0][0])
        assert sent["event"] == "cold_fill_load"
        assert sent["segment_id"] == "g1:105:cp.2:goal.0"

    async def test_returns_true_when_queue_empty(self, tcp, db):
        cc = CaptureController()
        await cc.start_cold_fill("g1", tcp, db)

        # Process both segments
        await cc.handle_cold_fill_spawn(
            {"state_captured": True, "state_path": "/cold1.mss"}, tcp, db,
        )
        done = await cc.handle_cold_fill_spawn(
            {"state_captured": True, "state_path": "/cold2.mss"}, tcp, db,
        )
        assert done is True

    async def test_ignores_spawn_without_state(self, tcp, db):
        cc = CaptureController()
        await cc.start_cold_fill("g1", tcp, db)

        done = await cc.handle_cold_fill_spawn(
            {"state_captured": False}, tcp, db,
        )
        assert done is False
        # Queue unchanged — still on first segment
        assert cc.cold_fill_current == "g1:105:cp.1:cp.2"
