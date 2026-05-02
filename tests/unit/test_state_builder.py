"""Tests for StateBuilder — covers branches not already exercised by
test_dashboard_integration.py (which covers the practice branch).

Uses real SessionManager + real DB instead of mocking SessionManager
attributes, so tests break if the SM interface changes.
"""

from spinlab.models import Mode
from spinlab.session_manager import SessionManager
from spinlab.speed_run import SpeedRunSession


def _make_sm(db, tcp):
    return SessionManager(db=db, tcp=tcp, rom_dir=None, default_category="any%")


class TestIdleBaseCase:
    def test_no_game_returns_bare_state(self, practice_db, mock_tcp):
        sm = _make_sm(practice_db, mock_tcp)
        state = sm.get_state()

        assert state["mode"] == "idle"
        assert state["game_id"] is None
        assert state["game_name"] is None
        assert state["current_segment"] is None
        assert state["recent"] == []
        assert state["session"] is None
        assert state["allocator_weights"] is None
        assert state["estimator"] is None


class TestSpeedRunBranch:
    def test_speed_run_populates_current_level(self, practice_db, mock_tcp):
        sm = _make_sm(practice_db, mock_tcp)
        sm.game_id = "g"
        sm.game_name = "Game"

        sr = SpeedRunSession(tcp=mock_tcp, db=practice_db, game_id="g")
        sr.segments_recorded = 3
        sr.levels_completed = 2
        sm.speed_run_session = sr
        sm.mode = Mode.SPEED_RUN

        state = sm.get_state()

        assert state["mode"] == "speed_run"
        assert state["session"]["id"] == sr.session_id
        assert state["session"]["segments_attempted"] == 3
        assert state["session"]["segments_completed"] == 2
        # Real SpeedRunSession built its levels from DB — verify current level
        assert state["current_segment"]["level_number"] == 1
        assert state["current_segment"]["state_path"] is not None


class TestColdFillBranch:
    def test_cold_fill_includes_state(self, practice_db, mock_tcp):
        sm = _make_sm(practice_db, mock_tcp)
        sm.game_id = "g"
        sm.game_name = "Game"
        sm.mode = Mode.COLD_FILL

        # Drive the real ColdFillController into mid-fill state
        sm.cold_fill.queue = [
            {"segment_id": "seg1", "hot_state_path": "/hot1.mss",
             "level_number": 105, "start_type": "checkpoint", "start_ordinal": 1,
             "end_type": "checkpoint", "end_ordinal": 2, "description": ""},
            {"segment_id": "seg2", "hot_state_path": "/hot2.mss",
             "level_number": 105, "start_type": "checkpoint", "start_ordinal": 2,
             "end_type": "goal", "end_ordinal": 0, "description": ""},
        ]
        sm.cold_fill.current = "seg1"
        sm.cold_fill.total = 5

        state = sm.get_state()
        assert state["mode"] == "cold_fill"
        # current_num = total - len(queue) + 1 = 5 - 2 + 1 = 4
        assert state["cold_fill"]["current"] == 4
        assert state["cold_fill"]["total"] == 5

    def test_cold_fill_none_state_omitted(self, practice_db, mock_tcp):
        """When cold_fill has no current segment, no cold_fill key is added."""
        sm = _make_sm(practice_db, mock_tcp)
        sm.game_id = "g"
        sm.game_name = "Game"
        sm.mode = Mode.COLD_FILL
        # cold_fill.current is None by default → get_state() returns None

        state = sm.get_state()
        assert "cold_fill" not in state


class TestSectionsCaptured:
    def test_sections_captured_none_when_idle(self, practice_db, mock_tcp):
        """sections_captured is None when no active recording session."""
        sm = _make_sm(practice_db, mock_tcp)
        sm.game_id = "g"
        sm.game_name = "Game"
        # No capture_run_id set → None
        state = sm.get_state()
        assert state["sections_captured"] is None

    def test_sections_captured_count_when_recording(self, practice_db, mock_tcp):
        """sections_captured reflects DB segment count for the active capture run."""
        from spinlab.models import Mode

        sm = _make_sm(practice_db, mock_tcp)
        sm.game_id = "g"
        sm.game_name = "Game"
        sm.mode = Mode.REFERENCE

        # Create a draft capture_run and associate it with the recorder
        run_id = "run-test-count"
        practice_db.create_capture_run(run_id, "g", "Test", draft=True)

        # Add a segment linked to this run
        from spinlab.models import Segment, Waypoint
        wp_s = Waypoint.make("g", 2, "entrance", 0, {})
        wp_e = Waypoint.make("g", 2, "goal", 0, {})
        practice_db.upsert_waypoint(wp_s)
        practice_db.upsert_waypoint(wp_e)
        seg = Segment(
            id=Segment.make_id("g", 2, "entrance", 0, "goal", 0, wp_s.id, wp_e.id),
            game_id="g", level_number=2,
            start_type="entrance", start_ordinal=0,
            end_type="goal", end_ordinal=0,
            start_waypoint_id=wp_s.id, end_waypoint_id=wp_e.id,
            ordinal=1,
            reference_id=run_id,
        )
        practice_db.upsert_segment(seg)

        sm.capture.recorder.capture_run_id = run_id
        state = sm.get_state()
        assert state["sections_captured"] == 1


class TestDraftBranch:
    def test_paused_run_state_included_when_active(self, practice_db, mock_tcp):
        sm = _make_sm(practice_db, mock_tcp)
        sm.game_id = "g"
        sm.game_name = "Game"

        # Create a capture run in the DB and set as paused
        practice_db.create_capture_run("run-xyz", "g", "Test Run", draft=True)
        sm.capture.paused_run_id = "run-xyz"

        state = sm.get_state()
        assert state["paused_run"]["run_id"] == "run-xyz"
        assert state["paused_run"]["segments_captured"] == 0
