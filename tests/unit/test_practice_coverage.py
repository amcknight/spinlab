"""Additional practice.py coverage — run_loop lifecycle, callbacks, edge cases."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from spinlab.practice import PracticeSession
from spinlab.protocol import AttemptResultEvent, PracticeLoadCmd, PracticeStopCmd
from spinlab.scheduler import Scheduler


def _make_tcp():
    emu = MagicMock()
    emu.is_connected = True
    emu.send = AsyncMock()
    emu.send_command = AsyncMock()
    return emu
class TestRunLoopLifecycle:
    @pytest.mark.asyncio
    async def test_run_loop_creates_and_ends_session(self, practice_db):
        seg_id = practice_db._test_seg_id
        emu = _make_tcp()
        ps = PracticeSession(emu=emu, db=practice_db, game_id="g", scheduler=Scheduler(practice_db, "g"))

        # Deliver a result then stop
        async def deliver_and_stop():
            await asyncio.sleep(0.05)
            ps.receive_result(AttemptResultEvent(
                segment_id=seg_id,
                completed=True,
                time_ms=5000,
            ))
            await asyncio.sleep(0.05)
            ps.is_running = False

        asyncio.create_task(deliver_and_stop())
        await ps.run_loop()

        # Session was created and ended in DB
        sessions = practice_db.get_session_history("g")
        assert len(sessions) == 1
        assert sessions[0]["segments_attempted"] == 1

    @pytest.mark.asyncio
    async def test_run_loop_sends_practice_stop_on_exit(self, practice_db):
        emu = _make_tcp()
        ps = PracticeSession(emu=emu, db=practice_db, game_id="g", scheduler=Scheduler(practice_db, "g"))

        # Stop immediately
        async def stop_soon():
            await asyncio.sleep(0.05)
            ps.is_running = False

        asyncio.create_task(stop_soon())
        await ps.run_loop()

        # Last backend command should be practice_stop
        cmds = [c[0][0] for c in emu.send_command.call_args_list]
        assert any(isinstance(c, PracticeStopCmd) for c in cmds)


class TestOnAttemptCallback:
    @pytest.mark.asyncio
    async def test_callback_fires_on_result(self, practice_db):
        seg_id = practice_db._test_seg_id
        emu = _make_tcp()
        received = []
        ps = PracticeSession(
            emu=emu, db=practice_db, game_id="g",
            scheduler=Scheduler(practice_db, "g"),
            on_attempt=lambda a: received.append(a),
        )
        ps.is_running = True

        async def deliver():
            await asyncio.sleep(0.05)
            ps.receive_result(AttemptResultEvent(
                segment_id=seg_id,
                completed=True,
                time_ms=4500,
            ))

        asyncio.create_task(deliver())
        await ps.run_one()

        assert len(received) == 1
        assert received[0].segment_id == seg_id
        assert received[0].completed is True
class TestDisconnectDuringWait:
    @pytest.mark.asyncio
    async def test_run_one_exits_on_disconnect(self, practice_db):
        emu = _make_tcp()
        ps = PracticeSession(emu=emu, db=practice_db, game_id="g", scheduler=Scheduler(practice_db, "g"))
        ps.is_running = True

        async def disconnect():
            await asyncio.sleep(0.1)
            emu.is_connected = False

        asyncio.create_task(disconnect())
        result = await asyncio.wait_for(ps.run_one(), timeout=5.0)
        # Should return True (segment was picked) but no result processed
        assert result is True
        assert ps.segments_attempted == 0  # no result arrived


class TestOverlayLabelGeneration:
    @pytest.mark.asyncio
    async def test_auto_label_entrance_to_goal(self, practice_db):
        seg_id = practice_db._test_seg_id
        # Clear description so auto-label kicks in
        practice_db.update_segment(seg_id, description="")
        emu = _make_tcp()
        ps = PracticeSession(emu=emu, db=practice_db, game_id="g", scheduler=Scheduler(practice_db, "g"))
        ps.is_running = True

        async def deliver():
            await asyncio.sleep(0.05)
            ps.receive_result(AttemptResultEvent(
                segment_id=seg_id,
                completed=True,
                time_ms=5000,
            ))

        asyncio.create_task(deliver())
        await ps.run_one()

        cmd = emu.send_command.call_args[0][0]
        assert isinstance(cmd, PracticeLoadCmd)
        # Segment has no description, so auto-generated: "L1 start > goal"
        assert cmd.description == "L1 start > goal"

    @pytest.mark.asyncio
    async def test_custom_description_used_when_present(self, practice_db):
        seg_id = practice_db._test_seg_id
        # Update segment to have a description
        practice_db.update_segment(seg_id, description="My custom segment")
        emu = _make_tcp()
        ps = PracticeSession(emu=emu, db=practice_db, game_id="g", scheduler=Scheduler(practice_db, "g"))
        ps.is_running = True

        async def deliver():
            await asyncio.sleep(0.05)
            ps.receive_result(AttemptResultEvent(
                segment_id=seg_id,
                completed=True,
                time_ms=5000,
            ))

        asyncio.create_task(deliver())
        await ps.run_one()

        cmd = emu.send_command.call_args[0][0]
        assert isinstance(cmd, PracticeLoadCmd)
        assert cmd.description == "My custom segment"
