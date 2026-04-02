# tests/test_session_manager.py
"""Tests for SessionManager state machine logic.

Keeps: mode transition guards, event routing, reference capture state machine.
Removed: mock-wiring-only tests covered by dashboard integration tests.
"""
from unittest.mock import MagicMock

import pytest

from spinlab.models import Mode, Segment, SegmentVariant
from spinlab.session_manager import SessionManager


def make_sm(mock_db, mock_tcp, **kwargs):
    defaults = dict(db=mock_db, tcp=mock_tcp, rom_dir=None, default_category="any%")
    defaults.update(kwargs)
    return SessionManager(**defaults)


class TestInit:
    def test_initial_state(self, mock_db, mock_tcp):
        sm = make_sm(mock_db, mock_tcp)
        assert sm.mode == Mode.IDLE
        assert sm.game_id is None
        assert sm.scheduler is None
        assert sm.practice_session is None


class TestEventRouting:
    async def test_rom_info_discovers_game(self, mock_db, mock_tcp, tmp_path):
        rom_file = tmp_path / "test_hack.sfc"
        rom_file.write_bytes(b"\x00" * 1024)

        sm = make_sm(mock_db, mock_tcp, rom_dir=tmp_path)
        await sm.route_event({"event": "rom_info", "filename": "test_hack.sfc"})
        assert sm.game_id is not None
        assert sm.game_name is not None

    async def test_rom_info_no_rom_dir(self, mock_db, mock_tcp):
        sm = make_sm(mock_db, mock_tcp)
        await sm.route_event({"event": "rom_info", "filename": "test.sfc"})
        assert sm.game_id is None

    async def test_game_context_switches_game(self, mock_db, mock_tcp):
        sm = make_sm(mock_db, mock_tcp)
        await sm.route_event({
            "event": "game_context",
            "game_id": "abc123",
            "game_name": "Test Game",
        })
        assert sm.game_id == "abc123"
        assert sm.game_name == "Test Game"

    async def test_events_ignored_outside_reference(self, mock_db, mock_tcp):
        sm = make_sm(mock_db, mock_tcp)
        sm.game_id = "game1"
        sm.mode = Mode.IDLE

        await sm.route_event({"event": "level_entrance", "level": 1, "room": 0})
        await sm.route_event({"event": "level_exit", "level": 1, "room": 0, "goal": "normal"})
        assert sm.ref_capture.pending_start is None
        assert sm.ref_capture.segments_count == 0


class TestModeGuards:
    async def test_start_reference_during_practice(self, mock_db, mock_tcp):
        sm = make_sm(mock_db, mock_tcp)
        sm.game_id = "game1"
        sm.mode = Mode.PRACTICE
        result = await sm.start_reference()
        assert result["status"] == "practice_active"

    async def test_on_practice_done_sets_idle(self, mock_db, mock_tcp):
        sm = make_sm(mock_db, mock_tcp)
        sm.mode = Mode.PRACTICE
        sm._on_practice_done(MagicMock())
        assert sm.mode == Mode.IDLE


class TestReferenceCapture:
    async def test_entrance_buffered(self, mock_db, mock_tcp):
        sm = make_sm(mock_db, mock_tcp)
        sm.game_id = "game1"
        sm.mode = Mode.REFERENCE
        sm.ref_capture.capture_run_id = "run1"

        await sm.route_event({
            "event": "level_entrance", "level": 105, "room": 0,
            "state_path": "/path/to/state.mss",
        })
        assert sm.ref_capture.pending_start is not None
        assert sm.ref_capture.pending_start["level_num"] == 105

    async def test_exit_pairs_with_entrance(self, mock_db, mock_tcp):
        sm = make_sm(mock_db, mock_tcp)
        sm.game_id = "game1"
        sm.mode = Mode.REFERENCE
        sm.ref_capture.capture_run_id = "run1"

        await sm.route_event({
            "event": "level_entrance", "level": 105, "room": 0,
            "state_path": "/path/to/state.mss",
        })
        await sm.route_event({
            "event": "level_exit", "level": 105, "room": 0,
            "goal": "normal", "elapsed_ms": 5000,
        })
        assert sm.ref_capture.segments_count == 1

    async def test_exit_pairs_across_rooms(self, mock_db, mock_tcp):
        sm = make_sm(mock_db, mock_tcp)
        sm.game_id = "game1"
        sm.mode = Mode.REFERENCE
        sm.ref_capture.capture_run_id = "run1"

        await sm.route_event({
            "event": "level_entrance", "level": 105, "room": 0,
            "state_path": "/path/to/state.mss",
        })
        await sm.route_event({
            "event": "level_exit", "level": 105, "room": 5,
            "goal": "normal", "elapsed_ms": 8000,
        })
        assert sm.ref_capture.segments_count == 1
        seg = mock_db.upsert_segment.call_args[0][0]
        assert seg.level_number == 105

    async def test_abort_discards_entrance(self, mock_db, mock_tcp):
        sm = make_sm(mock_db, mock_tcp)
        sm.game_id = "game1"
        sm.mode = Mode.REFERENCE

        await sm.route_event({"event": "level_entrance", "level": 105, "room": 0})
        await sm.route_event({"event": "level_exit", "level": 105, "room": 0, "goal": "abort"})
        assert sm.ref_capture.segments_count == 0

    async def test_checkpoint_creates_segment(self, mock_db, mock_tcp):
        sm = make_sm(mock_db, mock_tcp)
        sm.game_id = "game1"
        await sm.start_reference()

        await sm.route_event({
            "event": "level_entrance", "level": 105, "room": 0,
            "state_path": "/states/105_entrance.mss",
        })
        await sm.route_event({
            "event": "checkpoint", "level_num": 105,
            "cp_type": "midway", "cp_ordinal": 1,
            "timestamp_ms": 5000, "state_path": "/states/105_cp1_hot.mss",
        })

        assert sm.ref_capture.segments_count == 1
        seg = mock_db.upsert_segment.call_args[0][0]
        assert seg.start_type == "entrance"
        assert seg.end_type == "checkpoint"
        assert seg.end_ordinal == 1
        assert sm.ref_capture.pending_start["type"] == "checkpoint"

    async def test_checkpoint_then_exit_creates_two_segments(self, mock_db, mock_tcp):
        sm = make_sm(mock_db, mock_tcp)
        sm.game_id = "game1"
        await sm.start_reference()

        await sm.route_event({
            "event": "level_entrance", "level": 105, "room": 0,
            "state_path": "/states/105_entrance.mss",
        })
        await sm.route_event({
            "event": "checkpoint", "level_num": 105,
            "cp_type": "midway", "cp_ordinal": 1,
            "timestamp_ms": 5000, "state_path": "/states/105_cp1_hot.mss",
        })
        await sm.route_event({
            "event": "level_exit", "level": 105, "room": 0,
            "goal": "normal", "elapsed_ms": 10000,
        })

        assert sm.ref_capture.segments_count == 2
        seg2 = mock_db.upsert_segment.call_args_list[1][0][0]
        assert seg2.start_type == "checkpoint"
        assert seg2.end_type == "goal"

    async def test_death_sets_ref_died(self, mock_db, mock_tcp):
        sm = make_sm(mock_db, mock_tcp)
        sm.game_id = "game1"
        sm.mode = Mode.REFERENCE
        await sm.route_event({"event": "death"})
        assert sm.ref_capture.died is True

    async def test_entrance_clears_ref_died(self, mock_db, mock_tcp):
        sm = make_sm(mock_db, mock_tcp)
        sm.game_id = "game1"
        sm.mode = Mode.REFERENCE
        sm.ref_capture.capture_run_id = "run1"
        sm.ref_capture.died = True

        await sm.route_event({
            "event": "level_entrance", "level": 105, "room": 0,
            "state_path": "/states/105.mss",
        })
        assert sm.ref_capture.died is False


class TestFillGap:
    async def test_fill_gap_loads_hot_and_captures_cold(self, mock_db, mock_tcp):
        sm = make_sm(mock_db, mock_tcp)
        sm.game_id = "game1"
        sm.ref_capture.capture_run_id = "run1"

        seg = Segment(
            id=Segment.make_id("game1", 105, "checkpoint", 1, "goal", 0),
            game_id="game1", level_number=105,
            start_type="checkpoint", start_ordinal=1,
            end_type="goal", end_ordinal=0,
            reference_id="run1",
        )
        mock_db.upsert_segment(seg)

        hot_variant = SegmentVariant(seg.id, "hot", "/hot.mss", False)
        mock_db.add_variant(hot_variant)
        mock_db.get_variant = MagicMock(side_effect=lambda sid, vt: hot_variant if vt == "hot" else None)
        mock_db.get_variants = MagicMock(return_value=[hot_variant])

        result = await sm.start_fill_gap(seg.id)
        assert result["status"] == "started"

        await sm.route_event({
            "event": "spawn", "level_num": 105,
            "is_cold_cp": True, "cp_ordinal": 1,
            "timestamp_ms": 1000, "state_captured": True,
            "state_path": "/cold.mss",
        })

        cold_calls = [c for c in mock_db.add_variant.call_args_list if c[0][0].variant_type == "cold"]
        assert len(cold_calls) == 1
        assert cold_calls[0][0][0].state_path == "/cold.mss"
        assert sm.fill_gap_segment_id is None
        assert sm.mode == Mode.IDLE


class TestColdFillMode:
    def test_cold_fill_mode_exists(self):
        assert Mode.COLD_FILL.value == "cold_fill"

    def test_idle_to_cold_fill_legal(self):
        from spinlab.models import transition_mode
        result = transition_mode(Mode.IDLE, Mode.COLD_FILL)
        assert result == Mode.COLD_FILL

    def test_cold_fill_to_idle_legal(self):
        from spinlab.models import transition_mode
        result = transition_mode(Mode.COLD_FILL, Mode.IDLE)
        assert result == Mode.IDLE

    def test_cold_fill_to_practice_illegal(self):
        from spinlab.models import transition_mode
        with pytest.raises(ValueError):
            transition_mode(Mode.COLD_FILL, Mode.PRACTICE)
