# tests/test_session_manager.py
"""Tests for SessionManager state machine."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from spinlab.session_manager import SessionManager


def make_mock_tcp():
    tcp = MagicMock()
    tcp.is_connected = True
    tcp.send = AsyncMock()
    tcp.recv_event = AsyncMock(return_value=None)
    return tcp


def make_mock_db():
    db = MagicMock()
    db.upsert_game = MagicMock()
    db.create_session = MagicMock()
    db.end_session = MagicMock()
    db.create_capture_run = MagicMock()
    db.set_active_capture_run = MagicMock()
    db.get_recent_attempts = MagicMock(return_value=[])
    db.get_all_segments_with_model = MagicMock(return_value=[])
    db.load_model_state = MagicMock(return_value=None)
    db.load_allocator_config = MagicMock(return_value=None)
    db.upsert_segment = MagicMock()
    db.add_variant = MagicMock()
    db.get_active_segments = MagicMock(return_value=[])
    return db


class TestSessionManagerInit:
    def test_initial_state(self):
        sm = SessionManager(
            db=make_mock_db(),
            tcp=make_mock_tcp(),
            rom_dir=None,
            default_category="any%",
        )
        assert sm.mode == "idle"
        assert sm.game_id is None
        assert sm.game_name is None
        assert sm.scheduler is None
        assert sm.practice_session is None
        assert sm.practice_task is None

    def test_get_state_no_game(self):
        sm = SessionManager(
            db=make_mock_db(),
            tcp=make_mock_tcp(),
            rom_dir=None,
            default_category="any%",
        )
        state = sm.get_state()
        assert state["mode"] == "idle"
        assert state["game_id"] is None
        assert state["tcp_connected"] is True


class TestRouteEvent:
    @pytest.mark.asyncio
    async def test_rom_info_discovers_game(self, tmp_path):
        """rom_info event triggers game discovery via checksum."""
        rom_file = tmp_path / "test_hack.sfc"
        rom_file.write_bytes(b"\x00" * 1024)  # dummy ROM

        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=tmp_path, default_category="any%")

        await sm.route_event({"event": "rom_info", "filename": "test_hack.sfc"})

        assert sm.game_id is not None
        assert sm.game_name is not None
        db.upsert_game.assert_called_once()
        tcp.send.assert_called_once()  # game_context sent back

    @pytest.mark.asyncio
    async def test_rom_info_no_rom_dir(self):
        """rom_info with no rom_dir uses fallback ID."""
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=None, default_category="any%")

        await sm.route_event({"event": "rom_info", "filename": "test.sfc"})
        # No rom_dir → no game discovery
        assert sm.game_id is None

    @pytest.mark.asyncio
    async def test_game_context_switches_game(self):
        """game_context event triggers switch_game."""
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=None, default_category="any%")

        await sm.route_event({
            "event": "game_context",
            "game_id": "abc123",
            "game_name": "Test Game",
        })

        assert sm.game_id == "abc123"
        assert sm.game_name == "Test Game"

    @pytest.mark.asyncio
    async def test_level_entrance_in_reference_mode(self):
        """level_entrance buffered during reference mode."""
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=None, default_category="any%")
        sm.game_id = "game1"
        sm.mode = "reference"
        sm.ref_capture_run_id = "run1"

        await sm.route_event({
            "event": "level_entrance",
            "level": 105,
            "room": 0,
            "state_path": "/path/to/state.mss",
        })

        assert 105 in sm.ref_pending

    @pytest.mark.asyncio
    async def test_level_exit_pairs_with_entrance(self):
        """level_exit in reference mode pairs with pending entrance to create split."""
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=None, default_category="any%")
        sm.game_id = "game1"
        sm.mode = "reference"
        sm.ref_capture_run_id = "run1"

        # Buffer entrance
        await sm.route_event({
            "event": "level_entrance",
            "level": 105,
            "room": 0,
            "state_path": "/path/to/state.mss",
        })

        # Exit with goal
        await sm.route_event({
            "event": "level_exit",
            "level": 105,
            "room": 0,
            "goal": "normal",
            "elapsed_ms": 5000,
        })

        assert sm.ref_segments_count == 1
        db.upsert_segment.assert_called_once()

    @pytest.mark.asyncio
    async def test_level_exit_pairs_across_rooms(self):
        """Exit from a different room than entrance still pairs (sublevel case)."""
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=None, default_category="any%")
        sm.game_id = "game1"
        sm.mode = "reference"
        sm.ref_capture_run_id = "run1"

        # Entrance in room 0
        await sm.route_event({
            "event": "level_entrance",
            "level": 105,
            "room": 0,
            "state_path": "/path/to/state.mss",
        })

        # Exit from room 5 (player went through a pipe)
        await sm.route_event({
            "event": "level_exit",
            "level": 105,
            "room": 5,
            "goal": "normal",
            "elapsed_ms": 8000,
        })

        assert sm.ref_segments_count == 1
        db.upsert_segment.assert_called_once()
        # Segment should use entrance level number
        seg = db.upsert_segment.call_args[0][0]
        assert seg.level_number == 105

    @pytest.mark.asyncio
    async def test_level_exit_abort_discards(self):
        """level_exit with goal=abort discards pending entrance."""
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=None, default_category="any%")
        sm.game_id = "game1"
        sm.mode = "reference"

        await sm.route_event({
            "event": "level_entrance",
            "level": 105,
            "room": 0,
        })
        await sm.route_event({
            "event": "level_exit",
            "level": 105,
            "room": 0,
            "goal": "abort",
        })

        assert sm.ref_segments_count == 0
        db.upsert_segment.assert_not_called()

    @pytest.mark.asyncio
    async def test_events_ignored_outside_reference(self):
        """level_entrance/exit ignored when not in reference mode."""
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=None, default_category="any%")
        sm.game_id = "game1"
        sm.mode = "idle"

        await sm.route_event({"event": "level_entrance", "level": 1, "room": 0})
        await sm.route_event({"event": "level_exit", "level": 1, "room": 0, "goal": "normal"})

        assert len(sm.ref_pending) == 0
        assert sm.ref_segments_count == 0


    @pytest.mark.asyncio
    async def test_reference_checkpoint_creates_segment(self):
        """Checkpoint event during reference creates entrance->cp segment."""
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=None, default_category="any%")
        sm.game_id = "game1"

        # Start reference
        await sm.start_reference()

        # Simulate entrance
        await sm.route_event({
            "event": "level_entrance",
            "level": 105,
            "room": 0,
            "state_path": "/states/105_entrance.mss",
        })

        # Simulate checkpoint
        await sm.route_event({
            "event": "checkpoint",
            "level_num": 105,
            "cp_type": "midway",
            "cp_ordinal": 1,
            "timestamp_ms": 5000,
            "state_path": "/states/105_cp1_hot.mss",
        })

        # Should have created entrance.0->checkpoint.1 segment
        assert sm.ref_segments_count == 1
        db.upsert_segment.assert_called_once()
        seg = db.upsert_segment.call_args[0][0]
        assert seg.start_type == "entrance"
        assert seg.start_ordinal == 0
        assert seg.end_type == "checkpoint"
        assert seg.end_ordinal == 1

        # Hot variant should have been stored
        db.add_variant.assert_called_once()
        variant = db.add_variant.call_args[0][0]
        assert variant.variant_type == "hot"
        assert variant.state_path == "/states/105_cp1_hot.mss"

        # ref_pending_start should now be the checkpoint
        assert sm.ref_pending_start["type"] == "checkpoint"
        assert sm.ref_pending_start["ordinal"] == 1

    @pytest.mark.asyncio
    async def test_reference_checkpoint_then_exit(self):
        """Checkpoint followed by exit creates two segments."""
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=None, default_category="any%")
        sm.game_id = "game1"

        await sm.start_reference()

        # Entrance -> checkpoint -> exit
        await sm.route_event({
            "event": "level_entrance",
            "level": 105,
            "room": 0,
            "state_path": "/states/105_entrance.mss",
        })
        await sm.route_event({
            "event": "checkpoint",
            "level_num": 105,
            "cp_type": "midway",
            "cp_ordinal": 1,
            "timestamp_ms": 5000,
            "state_path": "/states/105_cp1_hot.mss",
        })
        await sm.route_event({
            "event": "level_exit",
            "level": 105,
            "room": 0,
            "goal": "normal",
            "elapsed_ms": 10000,
        })

        assert sm.ref_segments_count == 2
        assert db.upsert_segment.call_count == 2

        # Second segment should start from checkpoint
        seg2 = db.upsert_segment.call_args_list[1][0][0]
        assert seg2.start_type == "checkpoint"
        assert seg2.start_ordinal == 1
        assert seg2.end_type == "goal"

    @pytest.mark.asyncio
    async def test_death_sets_ref_died(self):
        """Death event during reference sets ref_died flag."""
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=None, default_category="any%")
        sm.game_id = "game1"
        sm.mode = "reference"

        await sm.route_event({"event": "death"})
        assert sm.ref_died is True

    @pytest.mark.asyncio
    async def test_entrance_clears_ref_died(self):
        """New entrance clears ref_died flag."""
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=None, default_category="any%")
        sm.game_id = "game1"
        sm.mode = "reference"
        sm.ref_capture_run_id = "run1"
        sm.ref_died = True

        await sm.route_event({
            "event": "level_entrance",
            "level": 105,
            "room": 0,
            "state_path": "/states/105.mss",
        })
        assert sm.ref_died is False


class TestReferenceMode:
    @pytest.mark.asyncio
    async def test_start_reference(self):
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=None, default_category="any%")
        sm.game_id = "game1"

        result = await sm.start_reference()

        assert result["status"] == "started"
        assert sm.mode == "reference"
        assert sm.ref_capture_run_id is not None
        db.create_capture_run.assert_called_once()

    @pytest.mark.asyncio
    async def test_start_reference_no_game(self):
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=None, default_category="any%")

        with pytest.raises(Exception):  # HTTPException
            await sm.start_reference()

    @pytest.mark.asyncio
    async def test_start_reference_during_practice(self):
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=None, default_category="any%")
        sm.game_id = "game1"
        sm.mode = "practice"

        result = await sm.start_reference()
        assert result["status"] == "practice_active"

    @pytest.mark.asyncio
    async def test_start_reference_not_connected(self):
        db = make_mock_db()
        tcp = make_mock_tcp()
        tcp.is_connected = False
        sm = SessionManager(db=db, tcp=tcp, rom_dir=None, default_category="any%")
        sm.game_id = "game1"

        result = await sm.start_reference()
        assert result["status"] == "not_connected"

    @pytest.mark.asyncio
    async def test_stop_reference(self):
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=None, default_category="any%")
        sm.game_id = "game1"
        await sm.start_reference()

        result = await sm.stop_reference()
        assert result["status"] == "stopped"
        assert sm.mode == "idle"


class TestPracticeMode:
    @pytest.mark.asyncio
    async def test_start_practice(self):
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=None, default_category="any%")
        sm.game_id = "game1"

        result = await sm.start_practice()
        assert result["status"] == "started"
        assert sm.mode == "practice"
        assert sm.practice_session is not None

    @pytest.mark.asyncio
    async def test_stop_practice(self):
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=None, default_category="any%")
        sm.game_id = "game1"
        await sm.start_practice()

        result = await sm.stop_practice()
        assert result["status"] == "stopped"
        assert sm.mode == "idle"

    @pytest.mark.asyncio
    async def test_start_practice_not_connected(self):
        db = make_mock_db()
        tcp = make_mock_tcp()
        tcp.is_connected = False
        sm = SessionManager(db=db, tcp=tcp, rom_dir=None, default_category="any%")
        sm.game_id = "game1"

        result = await sm.start_practice()
        assert result["status"] == "not_connected"


class TestAttemptResultRouting:
    @pytest.mark.asyncio
    async def test_attempt_result_delivered_to_practice_session(self):
        """attempt_result event must call receive_result on practice session."""
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=None, default_category="any%")
        sm.game_id = "game1"
        sm.mode = "practice"

        # Inject a mock practice session
        mock_ps = MagicMock()
        mock_ps.is_running = True
        sm.practice_session = mock_ps

        event = {
            "event": "attempt_result",
            "split_id": "game1:1:0:normal",
            "completed": True,
            "time_ms": 4500,
            "goal": "normal",
        }
        await sm.route_event(event)

        mock_ps.receive_result.assert_called_once_with(event)

    @pytest.mark.asyncio
    async def test_attempt_result_ignored_outside_practice(self):
        """attempt_result events should be ignored when not in practice mode."""
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=None, default_category="any%")
        sm.game_id = "game1"
        sm.mode = "idle"

        mock_ps = MagicMock()
        sm.practice_session = mock_ps

        await sm.route_event({
            "event": "attempt_result",
            "split_id": "s1",
            "completed": True,
            "time_ms": 4500,
        })

        mock_ps.receive_result.assert_not_called()

    @pytest.mark.asyncio
    async def test_attempt_result_sends_sse(self):
        """SSE subscribers should be notified when attempt_result arrives."""
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=None, default_category="any%")
        sm.game_id = "game1"
        sm.mode = "practice"

        mock_ps = MagicMock()
        mock_ps.is_running = True
        sm.practice_session = mock_ps

        q = sm.subscribe_sse()

        await sm.route_event({
            "event": "attempt_result",
            "split_id": "s1",
            "completed": True,
            "time_ms": 4500,
        })

        assert not q.empty()
        msg = q.get_nowait()
        assert msg["mode"] == "practice"


class TestPracticeDoneNotification:
    @pytest.mark.asyncio
    async def test_on_practice_done_sets_idle(self):
        """When practice task completes, mode should switch to idle."""
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=None, default_category="any%")
        sm.mode = "practice"

        mock_task = MagicMock()
        sm._on_practice_done(mock_task)

        assert sm.mode == "idle"

    @pytest.mark.asyncio
    async def test_on_practice_done_sends_sse(self):
        """SSE subscribers should be notified when practice ends naturally."""
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=None, default_category="any%")
        sm.mode = "practice"

        q = sm.subscribe_sse()

        mock_task = MagicMock()
        sm._on_practice_done(mock_task)

        # ensure_future schedules the coroutine — let the event loop run it
        await asyncio.sleep(0)

        assert not q.empty()
        msg = q.get_nowait()
        assert msg["mode"] == "idle"


class TestOnAttemptCallback:
    @pytest.mark.asyncio
    async def test_start_practice_wires_on_attempt_sse(self):
        """start_practice should wire on_attempt to push SSE."""
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=None, default_category="any%")
        sm.game_id = "game1"

        await sm.start_practice()

        assert sm.practice_session is not None
        assert sm.practice_session.on_attempt is not None

        # Call the on_attempt callback and check SSE fires
        q = sm.subscribe_sse()
        sm.practice_session.on_attempt(MagicMock())
        await asyncio.sleep(0)  # let ensure_future run

        assert not q.empty()


class TestFillGap:
    @pytest.mark.asyncio
    async def test_fill_gap_loads_hot_and_captures_cold(self):
        """Fill-gap mode loads hot CP state, captures cold on spawn."""
        from spinlab.models import Segment, SegmentVariant

        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=None, default_category="any%")
        sm.game_id = "game1"
        sm.ref_capture_run_id = "run1"

        # Create a segment with hot variant but no cold
        seg = Segment(
            id=Segment.make_id("game1", 105, "checkpoint", 1, "goal", 0),
            game_id="game1", level_number=105,
            start_type="checkpoint", start_ordinal=1,
            end_type="goal", end_ordinal=0,
            reference_id="run1",
        )
        db.upsert_segment(seg)

        hot_variant = SegmentVariant(seg.id, "hot", "/hot.mss", False)
        db.add_variant(hot_variant)
        # Mock get_variant to return the hot variant
        db.get_variant = MagicMock(side_effect=lambda sid, vt: hot_variant if vt == "hot" else None)
        db.get_variants = MagicMock(return_value=[hot_variant])

        result = await sm.start_fill_gap(seg.id)
        assert result["status"] == "started"
        assert sm.fill_gap_segment_id == seg.id

        # Simulate spawn with cold capture
        await sm.route_event({
            "event": "spawn",
            "level_num": 105,
            "is_cold_cp": True,
            "cp_ordinal": 1,
            "timestamp_ms": 1000,
            "state_captured": True,
            "state_path": "/cold.mss",
        })

        # Cold variant should have been added
        # Find the add_variant call for cold (skip the hot one we did in setup)
        cold_calls = [
            c for c in db.add_variant.call_args_list
            if c[0][0].variant_type == "cold"
        ]
        assert len(cold_calls) == 1
        assert cold_calls[0][0][0].state_path == "/cold.mss"
        assert cold_calls[0][0][0].is_default is True
        assert sm.fill_gap_segment_id is None  # fill-gap ended
        assert sm.mode == "idle"

    @pytest.mark.asyncio
    async def test_fill_gap_not_connected(self):
        """Fill-gap fails gracefully when TCP not connected."""
        db = make_mock_db()
        tcp = make_mock_tcp()
        tcp.is_connected = False
        sm = SessionManager(db=db, tcp=tcp, rom_dir=None, default_category="any%")
        sm.game_id = "game1"

        result = await sm.start_fill_gap("some_seg_id")
        assert result["status"] == "not_connected"

    @pytest.mark.asyncio
    async def test_fill_gap_no_hot_variant(self):
        """Fill-gap fails when segment has no hot variant."""
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=None, default_category="any%")
        sm.game_id = "game1"
        db.get_variant = MagicMock(return_value=None)

        result = await sm.start_fill_gap("some_seg_id")
        assert result["status"] == "no_hot_variant"


class TestSSE:
    @pytest.mark.asyncio
    async def test_subscribe_receives_notifications(self):
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=None, default_category="any%")

        q = sm.subscribe_sse()
        await sm._notify_sse()

        msg = q.get_nowait()
        assert msg["mode"] == "idle"

    @pytest.mark.asyncio
    async def test_unsubscribe_stops_notifications(self):
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=None, default_category="any%")

        q = sm.subscribe_sse()
        sm.unsubscribe_sse(q)
        await sm._notify_sse()

        assert q.empty()

    @pytest.mark.asyncio
    async def test_full_queue_drops_oldest(self):
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=None, default_category="any%")

        q = sm.subscribe_sse()
        # Fill queue
        for _ in range(16):
            await sm._notify_sse()
        # Should still accept new
        await sm._notify_sse()
        assert not q.empty()


class TestRecording:
    @pytest.mark.asyncio
    async def test_start_reference_sends_tcp_command(self, tmp_path):
        """start_reference sends reference_start with .spinrec path to Lua."""
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=tmp_path, default_category="any%", data_dir=tmp_path)
        sm.game_id = "abcdef0123456789"
        sm.game_name = "Test Game"

        result = await sm.start_reference()
        assert result["status"] == "started"
        assert sm.mode == "reference"

        # Verify TCP command was sent with path
        tcp.send.assert_called()
        sent = tcp.send.call_args_list[-1][0][0]
        import json
        msg = json.loads(sent)
        assert msg["event"] == "reference_start"
        assert msg["path"].endswith(".spinrec")

    @pytest.mark.asyncio
    async def test_stop_reference_sends_tcp_command(self, tmp_path):
        """stop_reference sends reference_stop to Lua."""
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=tmp_path, default_category="any%", data_dir=tmp_path)
        sm.game_id = "abcdef0123456789"
        sm.game_name = "Test Game"
        await sm.start_reference()
        tcp.send.reset_mock()

        result = await sm.stop_reference()
        assert result["status"] == "stopped"

        tcp.send.assert_called_once()
        import json
        msg = json.loads(tcp.send.call_args[0][0])
        assert msg["event"] == "reference_stop"

    @pytest.mark.asyncio
    async def test_rec_saved_event_stores_path(self, tmp_path):
        """rec_saved event from Lua stores .spinrec path on session."""
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=tmp_path, default_category="any%", data_dir=tmp_path)
        sm.game_id = "abcdef0123456789"
        sm.game_name = "Test Game"
        await sm.start_reference()

        await sm.route_event({"event": "rec_saved", "path": "/data/test.spinrec", "frame_count": 1000})
        assert sm.rec_path == "/data/test.spinrec"
