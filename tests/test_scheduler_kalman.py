"""Tests for the scheduler coordinator (multi-model)."""
import json
import pytest
from spinlab.db import Database
from spinlab.estimators import list_estimators
from spinlab.models import ModelOutput
from spinlab.scheduler import Scheduler


@pytest.fixture
def db_with_segments(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    db.upsert_game("g1", "Game", "any%")
    from spinlab.models import Segment, SegmentVariant
    states_dir = tmp_path / "states"
    states_dir.mkdir()
    for i, (start_type, end_type) in enumerate(
        [("entrance", "checkpoint"), ("checkpoint", "checkpoint"), ("checkpoint", "goal")],
        start=1,
    ):
        state_file = states_dir / f"{i}.mss"
        state_file.write_bytes(b"\x00" * 100)
        seg = Segment(
            id=f"g1:{i}:{start_type}.0:{end_type}.0",
            game_id="g1", level_number=i,
            start_type=start_type, start_ordinal=0,
            end_type=end_type, end_ordinal=0,
            description=f"Segment {i}", strat_version=1,
        )
        db.upsert_segment(seg)
        db.add_variant(SegmentVariant(
            segment_id=seg.id, variant_type="cold",
            state_path=str(state_file), is_default=True,
        ))
    return db


class TestSchedulerPickNext:
    def test_pick_next_returns_segment_with_model(self, db_with_segments):
        sched = Scheduler(db_with_segments, "g1")
        result = sched.pick_next()
        assert result is not None
        assert result.segment_id.startswith("g1:")

    def test_pick_next_no_segments_returns_none(self, tmp_path):
        db = Database(str(tmp_path / "test.db"))
        db.upsert_game("g1", "Game", "any%")
        sched = Scheduler(db, "g1")
        assert sched.pick_next() is None


class TestSchedulerProcessAttempt:
    def test_process_attempt_creates_all_model_states(self, db_with_segments):
        sched = Scheduler(db_with_segments, "g1")
        segment_id = "g1:1:entrance.0:checkpoint.0"
        sched.process_attempt(segment_id, time_ms=12000, completed=True)
        rows = db_with_segments.load_all_model_states_for_segment(segment_id)
        estimator_names = {r["estimator"] for r in rows}
        assert "kalman" in estimator_names
        assert "rolling_mean" in estimator_names
        try:
            import numpy  # noqa: F401
            assert "exp_decay" in estimator_names
        except ImportError:
            pass  # exp_decay unavailable without numpy
        for r in rows:
            out = ModelOutput.from_dict(json.loads(r["output_json"]))
            # exp_decay returns all None with < 3 points — that's correct
            if r["estimator"] != "exp_decay":
                assert out.total.expected_ms is not None or out.clean.expected_ms is not None

    def test_process_attempt_incomplete(self, db_with_segments):
        sched = Scheduler(db_with_segments, "g1")
        segment_id = "g1:1:entrance.0:checkpoint.0"
        sched.process_attempt(segment_id, time_ms=12000, completed=True)
        sched.process_attempt(segment_id, time_ms=5000, completed=False)
        row = db_with_segments.load_model_state(segment_id, "kalman")
        state = json.loads(row["state_json"])
        assert state["n_completed"] == 1
        assert state["n_attempts"] == 2

    def test_process_attempt_with_deaths(self, db_with_segments):
        sched = Scheduler(db_with_segments, "g1")
        segment_id = "g1:1:entrance.0:checkpoint.0"
        sched.process_attempt(
            segment_id, time_ms=12000, completed=True,
            deaths=3, clean_tail_ms=4000,
        )
        rows = db_with_segments.load_all_model_states_for_segment(segment_id)
        assert len(rows) == len(list_estimators())


class TestSchedulerPeek:
    def test_peek_next_n(self, db_with_segments):
        sched = Scheduler(db_with_segments, "g1")
        results = sched.peek_next_n(3)
        assert len(results) == 3
        assert all(isinstance(r, str) for r in results)


class TestSchedulerSwitch:
    def test_switch_allocator(self, db_with_segments):
        sched = Scheduler(db_with_segments, "g1")
        sched.switch_allocator("random")
        assert sched.allocator.name == "random"

    def test_switch_unknown_allocator_raises(self, db_with_segments):
        sched = Scheduler(db_with_segments, "g1")
        with pytest.raises(ValueError):
            sched.switch_allocator("nonexistent")


class TestSchedulerRebuild:
    def test_rebuild_all_states(self, db_with_segments):
        sched = Scheduler(db_with_segments, "g1")
        segment_id = "g1:1:entrance.0:checkpoint.0"
        sched.process_attempt(segment_id, time_ms=12000, completed=True)
        sched.process_attempt(segment_id, time_ms=11000, completed=True)
        sched.rebuild_all_states()
        rows = db_with_segments.load_all_model_states_for_segment(segment_id)
        assert len(rows) == len(list_estimators())


class TestStateFileFilter:
    def test_pick_next_skips_missing_state_files(self, tmp_path):
        from spinlab.models import Segment, SegmentVariant
        db = Database(":memory:")
        db.upsert_game("g1", "Test", "any%")
        valid_state = tmp_path / "valid.mss"
        valid_state.write_bytes(b"\x00" * 100)
        seg1 = Segment(id="s1", game_id="g1", level_number=1, start_type="entrance",
                        start_ordinal=0, end_type="checkpoint", end_ordinal=0)
        seg2 = Segment(id="s2", game_id="g1", level_number=2, start_type="entrance",
                        start_ordinal=0, end_type="checkpoint", end_ordinal=0)
        db.upsert_segment(seg1)
        db.upsert_segment(seg2)
        db.add_variant(SegmentVariant(segment_id="s1", variant_type="cold",
                                       state_path=str(valid_state), is_default=True))
        db.add_variant(SegmentVariant(segment_id="s2", variant_type="cold",
                                       state_path="/nonexistent/path.mss", is_default=True))
        sched = Scheduler(db, "g1")
        picked = sched.pick_next()
        assert picked is not None
        assert picked.segment_id == "s1"
