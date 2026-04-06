"""Tests for SystemState — single source of truth for system mode and sub-states."""
from spinlab.models import Mode
from spinlab.system_state import (
    CaptureState, ColdFillState, DraftState, FillGapState,
    PracticeState, SystemState,
)


class TestSystemStateDefaults:
    def test_defaults_to_idle_with_no_substates(self):
        state = SystemState()
        assert state.mode == Mode.IDLE
        assert state.game_id is None
        assert state.game_name is None
        assert state.capture is None
        assert state.draft is None
        assert state.cold_fill is None
        assert state.fill_gap is None
        assert state.practice is None


class TestSubStates:
    def test_capture_state(self):
        cs = CaptureState(run_id="run_abc")
        assert cs.run_id == "run_abc"
        assert cs.rec_path is None
        assert cs.segments_count == 0

    def test_draft_state(self):
        ds = DraftState(run_id="run_abc", segment_count=3)
        assert ds.run_id == "run_abc"
        assert ds.segment_count == 3

    def test_cold_fill_state(self):
        cfs = ColdFillState(
            current_segment_id="seg1", current_num=1,
            total=3, segment_label="L105 cp1 > cp2",
        )
        assert cfs.current_segment_id == "seg1"
        assert cfs.total == 3

    def test_fill_gap_state(self):
        fgs = FillGapState(segment_id="seg1", waypoint_id="wp1")
        assert fgs.segment_id == "seg1"

    def test_practice_state(self):
        ps = PracticeState(session_id="sess1", started_at="2026-01-01T00:00:00")
        assert ps.session_id == "sess1"
        assert ps.current_segment_id is None
        assert ps.segments_attempted == 0
        assert ps.segments_completed == 0
