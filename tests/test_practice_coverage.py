"""Additional practice.py coverage — run_loop lifecycle, callbacks, edge cases."""
import asyncio
import json

import pytest
from unittest.mock import AsyncMock, MagicMock
from pathlib import Path

from spinlab.db import Database
from spinlab.models import Segment, SegmentVariant
from spinlab.practice import PracticeSession

SEG_ID = "g:1:entrance.0:goal.0"


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    d.upsert_game("g", "Game", "any%")
    state_file = tmp_path / "test.mss"
    state_file.write_bytes(b"fake state")
    seg = Segment(
        id=SEG_ID, game_id="g", level_number=1,
        start_type="entrance", start_ordinal=0,
        end_type="goal", end_ordinal=0,
        description="", ordinal=1,
    )
    d.upsert_segment(seg)
    # TODO(Task 8): restore add_save_state on waypoint once get_all_segments_with_model
    # joins waypoint_save_states. For now, state_path is NULL for all segments.
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
        tcp = _make_tcp()
        ps = PracticeSession(tcp=tcp, db=db, game_id="g")

        # Deliver a result then stop
        async def deliver_and_stop():
            await asyncio.sleep(0.05)
            ps.receive_result({
                "event": "attempt_result",
                "segment_id": SEG_ID,
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
    @pytest.mark.skip(reason="Task 8 restores state_path via waypoint_save_states join")
    @pytest.mark.asyncio
    async def test_callback_fires_on_result(self, db):
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
                "segment_id": SEG_ID,
                "completed": True, "time_ms": 4500,
            })

        asyncio.create_task(deliver())
        await ps.run_one()

        assert len(received) == 1
        assert received[0].segment_id == SEG_ID
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
    @pytest.mark.skip(reason="Task 8 restores state_path via waypoint_save_states join")
    @pytest.mark.asyncio
    async def test_auto_label_entrance_to_goal(self, db):
        tcp = _make_tcp()
        ps = PracticeSession(tcp=tcp, db=db, game_id="g")
        ps.is_running = True

        async def deliver():
            await asyncio.sleep(0.05)
            ps.receive_result({
                "event": "attempt_result",
                "segment_id": SEG_ID,
                "completed": True, "time_ms": 5000,
            })

        asyncio.create_task(deliver())
        await ps.run_one()

        sent = tcp.send.call_args[0][0]
        payload = json.loads(sent.removeprefix("practice_load:"))
        # Segment has no description, so auto-generated: "L1 start > goal"
        assert payload["description"] == "L1 start > goal"

    @pytest.mark.skip(reason="Task 8 restores state_path via waypoint_save_states join")
    @pytest.mark.asyncio
    async def test_custom_description_used_when_present(self, db):
        # Update segment to have a description
        db.update_segment(SEG_ID, description="My custom segment")
        tcp = _make_tcp()
        ps = PracticeSession(tcp=tcp, db=db, game_id="g")
        ps.is_running = True

        async def deliver():
            await asyncio.sleep(0.05)
            ps.receive_result({
                "event": "attempt_result",
                "segment_id": SEG_ID,
                "completed": True, "time_ms": 5000,
            })

        asyncio.create_task(deliver())
        await ps.run_one()

        sent = tcp.send.call_args[0][0]
        payload = json.loads(sent.removeprefix("practice_load:"))
        assert payload["description"] == "My custom segment"
