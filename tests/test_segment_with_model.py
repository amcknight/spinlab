"""Tests for SegmentWithModel.load_all factory classmethod."""
import json
import pytest
from spinlab.allocators import SegmentWithModel
from spinlab.db import Database
from spinlab.models import Segment, Waypoint, WaypointSaveState


def _make_seg_with_state(db, game_id, level, start_type, end_type, state_path):
    """Create waypoints + segment + hot save state, return segment."""
    wp_start = Waypoint.make(game_id, level, start_type, 0, {})
    wp_end = Waypoint.make(game_id, level, end_type, 0, {"end": end_type})
    db.upsert_waypoint(wp_start)
    db.upsert_waypoint(wp_end)
    seg = Segment(
        id=Segment.make_id(game_id, level, start_type, 0, end_type, 0,
                           wp_start.id, wp_end.id),
        game_id=game_id, level_number=level,
        start_type=start_type, start_ordinal=0,
        end_type=end_type, end_ordinal=0,
        description=f"Segment {level}", strat_version=1,
        start_waypoint_id=wp_start.id, end_waypoint_id=wp_end.id,
    )
    db.upsert_segment(seg)
    db.add_save_state(WaypointSaveState(
        waypoint_id=wp_start.id, variant_type="hot",
        state_path=state_path, is_default=True,
    ))
    return seg


@pytest.fixture
def db_with_segments(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    db.upsert_game("g1", "Game", "any%")
    states_dir = tmp_path / "states"
    states_dir.mkdir()
    for i, (start_type, end_type) in enumerate(
        [("entrance", "checkpoint"), ("checkpoint", "checkpoint"), ("checkpoint", "goal")],
        start=1,
    ):
        state_file = states_dir / f"{i}.mss"
        state_file.write_bytes(b"\x00" * 100)
        _make_seg_with_state(db, "g1", i, start_type, end_type, str(state_file))
    return db


class TestLoadAll:
    def test_basic_assembly(self, db_with_segments):
        """3 segments with no model state yet."""
        results = SegmentWithModel.load_all(db_with_segments, "g1")
        assert len(results) == 3
        for s in results:
            assert isinstance(s, SegmentWithModel)
            assert s.game_id == "g1"
            assert s.model_outputs == {}
            assert s.n_completed == 0
            assert s.n_attempts == 0

    def test_empty_game(self, tmp_path):
        db = Database(str(tmp_path / "empty.db"))
        db.upsert_game("g2", "Empty", "any%")
        results = SegmentWithModel.load_all(db, "g2")
        assert results == []

    def test_with_model_state(self, db_with_segments):
        """Segments with saved model outputs get them populated."""
        from spinlab.models import ModelOutput, Estimate
        # Get the first segment id from DB
        rows = db_with_segments.get_all_segments_with_model("g1")
        seg_id = rows[0]["id"]
        out = ModelOutput(
            total=Estimate(expected_ms=12000.0, ms_per_attempt=-500.0, floor_ms=None),
            clean=Estimate(expected_ms=None, ms_per_attempt=None, floor_ms=None),
        )
        state_json = json.dumps({"mu": 12.0, "d": -0.5, "n_completed": 5, "n_attempts": 7})
        db_with_segments.save_model_state(seg_id, "kalman", state_json, json.dumps(out.to_dict()))

        results = SegmentWithModel.load_all(db_with_segments, "g1")
        seg1 = next(s for s in results if s.segment_id == seg_id)
        assert "kalman" in seg1.model_outputs
        assert seg1.model_outputs["kalman"].total.expected_ms == 12000.0
        assert seg1.n_completed == 5
        assert seg1.n_attempts == 7

    def test_with_golds(self, db_with_segments):
        """Gold times from attempts are populated."""
        from spinlab.models import Attempt
        rows = db_with_segments.get_all_segments_with_model("g1")
        seg_id = rows[0]["id"]
        db_with_segments.log_attempt(Attempt(
            segment_id=seg_id, session_id="s1", completed=True,
            time_ms=10000, deaths=0, clean_tail_ms=10000,
        ))
        results = SegmentWithModel.load_all(db_with_segments, "g1")
        seg1 = next(s for s in results if s.segment_id == seg_id)
        assert seg1.gold_ms == 10000

    def test_malformed_output_json_skipped(self, db_with_segments):
        """Bad JSON in output_json is skipped, not a crash."""
        rows = db_with_segments.get_all_segments_with_model("g1")
        seg_id = rows[0]["id"]
        state_json = json.dumps({"n_completed": 1, "n_attempts": 1})
        db_with_segments.save_model_state(seg_id, "kalman", state_json, "{bad json")
        results = SegmentWithModel.load_all(db_with_segments, "g1")
        seg1 = next(s for s in results if s.segment_id == seg_id)
        assert "kalman" not in seg1.model_outputs

    def test_selected_model_passthrough(self, db_with_segments):
        results = SegmentWithModel.load_all(db_with_segments, "g1", selected_model="rolling_mean")
        for s in results:
            assert s.selected_model == "rolling_mean"
