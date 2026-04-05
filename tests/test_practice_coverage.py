"""Additional practice.py coverage — run_loop lifecycle, callbacks, edge cases."""
import asyncio
import json

import pytest
from unittest.mock import AsyncMock, MagicMock
from pathlib import Path

from spinlab.db import Database
from spinlab.models import Segment, Waypoint, WaypointSaveState
from spinlab.practice import PracticeSession


def _make_seg_with_state(db, game_id, level, start_type, end_type,
                         state_path, ordinal=1):
    """Create waypoints + segment + hot save state; return segment."""
    wp_start = Waypoint.make(game_id, level, start_type, 0, {})
    wp_end = Waypoint.make(game_id, level, end_type, 0, {})
    db.upsert_waypoint(wp_start)
    db.upsert_waypoint(wp_end)
    seg = Segment(
        id=Segment.make_id(game_id, level, start_type, 0, end_type, 0,
                           wp_start.id, wp_end.id),
        game_id=game_id, level_number=level,
        start_type=start_type, start_ordinal=0,
        end_type=end_type, end_ordinal=0,
        description="",
        ordinal=ordinal,
        start_waypoint_id=wp_start.id, end_waypoint_id=wp_end.id,
    )
    db.upsert_segment(seg)
    db.add_save_state(WaypointSaveState(
        waypoint_id=wp_start.id, variant_type="hot",
        state_path=str(state_path), is_default=True,
    ))
    return seg


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    d.upsert_game("g", "Game", "any%")
    state_file = tmp_path / "test.mss"
    state_file.write_bytes(b"fake state")
    seg = _make_seg_with_state(d, "g", 1, "entrance", "goal", state_file)
    d._test_seg_id = seg.id
    return d


def _make_tcp():
    tcp = MagicMock()
    tcp.is_connected = True
    tcp.send = AsyncMock()
    return tcp


@pytest.mark.slow
class TestRunLoopLifecycle:
    @pytest.mark.asyncio
    async def test_run_loop_creates_and_ends_session(self, db):
        seg_id = db._test_seg_id
        tcp = _make_tcp()
        ps = PracticeSession(tcp=tcp, db=db, game_id="g")

        # Deliver a result then stop
        async def deliver_and_stop():
            await asyncio.sleep(0.05)
            ps.receive_result({
                "event": "attempt_result",
                "segment_id": seg_id,
                "completed": True, "time_ms": 5000,
            })
            await asyncio.sleep(0.05)
            ps.is_running = False

        asyncio.create_task(deliver_and_stop())
        await ps.run_loop()

        # Session was created and ended in DB
        sessions = db.get_session_history("g")
        assert len(sessions) == 1
        assert sessions[0]["segments_attempted"] == 1

    @pytest.mark.asyncio
    async def test_run_loop_sends_practice_stop_on_exit(self, db):
        tcp = _make_tcp()
        ps = PracticeSession(tcp=tcp, db=db, game_id="g")

        # Stop immediately
        async def stop_soon():
            await asyncio.sleep(0.05)
            ps.is_running = False

        asyncio.create_task(stop_soon())
        await ps.run_loop()

        # Last TCP message should be practice_stop
        calls = [c[0][0] for c in tcp.send.call_args_list]
        assert "practice_stop" in calls


class TestOnAttemptCallback:
    @pytest.mark.asyncio
    async def test_callback_fires_on_result(self, db):
        seg_id = db._test_seg_id
        tcp = _make_tcp()
        received = []
        ps = PracticeSession(
            tcp=tcp, db=db, game_id="g",
            on_attempt=lambda a: received.append(a),
        )
        ps.is_running = True

        async def deliver():
            await asyncio.sleep(0.05)
            ps.receive_result({
                "event": "attempt_result",
                "segment_id": seg_id,
                "completed": True, "time_ms": 4500,
            })

        asyncio.create_task(deliver())
        await ps.run_one()

        assert len(received) == 1
        assert received[0].segment_id == seg_id
        assert received[0].completed is True


@pytest.mark.slow
class TestDisconnectDuringWait:
    @pytest.mark.asyncio
    async def test_run_one_exits_on_disconnect(self, db):
        tcp = _make_tcp()
        ps = PracticeSession(tcp=tcp, db=db, game_id="g")
        ps.is_running = True

        async def disconnect():
            await asyncio.sleep(0.1)
            tcp.is_connected = False

        asyncio.create_task(disconnect())
        result = await asyncio.wait_for(ps.run_one(), timeout=5.0)
        # Should return True (segment was picked) but no result processed
        assert result is True
        assert ps.segments_attempted == 0  # no result arrived


class TestOverlayLabelGeneration:
    @pytest.mark.asyncio
    async def test_auto_label_entrance_to_goal(self, db):
        seg_id = db._test_seg_id
        tcp = _make_tcp()
        ps = PracticeSession(tcp=tcp, db=db, game_id="g")
        ps.is_running = True

        async def deliver():
            await asyncio.sleep(0.05)
            ps.receive_result({
                "event": "attempt_result",
                "segment_id": seg_id,
                "completed": True, "time_ms": 5000,
            })

        asyncio.create_task(deliver())
        await ps.run_one()

        sent = tcp.send.call_args[0][0]
        payload = json.loads(sent.removeprefix("practice_load:"))
        # Segment has no description, so auto-generated: "L1 start > goal"
        assert payload["description"] == "L1 start > goal"

    @pytest.mark.asyncio
    async def test_custom_description_used_when_present(self, db):
        seg_id = db._test_seg_id
        # Update segment to have a description
        db.update_segment(seg_id, description="My custom segment")
        tcp = _make_tcp()
        ps = PracticeSession(tcp=tcp, db=db, game_id="g")
        ps.is_running = True

        async def deliver():
            await asyncio.sleep(0.05)
            ps.receive_result({
                "event": "attempt_result",
                "segment_id": seg_id,
                "completed": True, "time_ms": 5000,
            })

        asyncio.create_task(deliver())
        await ps.run_one()

        sent = tcp.send.call_args[0][0]
        payload = json.loads(sent.removeprefix("practice_load:"))
        assert payload["description"] == "My custom segment"
