"""Tests for the scheduler coordinator (multi-model)."""
import json
import pytest
from spinlab.db import Database
from spinlab.estimators import list_estimators
from spinlab.models import ModelOutput, Segment, Waypoint, WaypointSaveState
from spinlab.scheduler import Scheduler


def _make_seg_with_state(db, game_id, level, start_type, end_type,
                         state_path, start_conds=None, end_conds=None):
    """Create waypoints + segment + hot save state; return segment."""
    start_conds = start_conds or {}
    end_conds = end_conds or {"e": end_type, "l": level}
    wp_start = Waypoint.make(game_id, level, start_type, 0, start_conds)
    wp_end = Waypoint.make(game_id, level, end_type, 0, end_conds)
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
        state_path=str(state_path), is_default=True,
    ))
    return seg


@pytest.fixture
def db_with_segments(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    db.upsert_game("g1", "Game", "any%")
    states_dir = tmp_path / "states"
    states_dir.mkdir()
    segs = []
    for i, (start_type, end_type) in enumerate(
        [("entrance", "checkpoint"), ("checkpoint", "checkpoint"), ("checkpoint", "goal")],
        start=1,
    ):
        state_file = states_dir / f"{i}.mss"
        state_file.write_bytes(b"\x00" * 100)
        seg = _make_seg_with_state(
            db, "g1", i, start_type, end_type, state_file,
            start_conds={"i": i},
        )
        segs.append(seg)
    db._test_segs = segs
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
        segment_id = db_with_segments._test_segs[0].id
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
        segment_id = db_with_segments._test_segs[0].id
        sched.process_attempt(segment_id, time_ms=12000, completed=True)
        sched.process_attempt(segment_id, time_ms=5000, completed=False)
        row = db_with_segments.load_model_state(segment_id, "kalman")
        state = json.loads(row["state_json"])
        assert state["n_completed"] == 1
        assert state["n_attempts"] == 2

    def test_process_attempt_with_deaths(self, db_with_segments):
        sched = Scheduler(db_with_segments, "g1")
        segment_id = db_with_segments._test_segs[0].id
        sched.process_attempt(
            segment_id, time_ms=12000, completed=True,
            deaths=3, clean_tail_ms=4000,
        )
        rows = db_with_segments.load_all_model_states_for_segment(segment_id)
        assert len(rows) == len(list_estimators())


class TestSchedulerWeights:
    def test_set_weights_persists_and_rebuilds(self, db_with_segments):
        sched = Scheduler(db_with_segments, "g1")
        sched.set_allocator_weights({"greedy": 50, "random": 50})
        raw = db_with_segments.load_allocator_config("allocator_weights")
        import json
        saved = json.loads(raw)
        assert saved == {"greedy": 50, "random": 50}

    def test_set_weights_invalid_sum_raises(self, db_with_segments):
        sched = Scheduler(db_with_segments, "g1")
        with pytest.raises(ValueError, match="must sum to 100"):
            sched.set_allocator_weights({"greedy": 50, "random": 30})

    def test_set_weights_unknown_allocator_raises(self, db_with_segments):
        sched = Scheduler(db_with_segments, "g1")
        with pytest.raises(ValueError, match="Unknown allocator"):
            sched.set_allocator_weights({"greedy": 50, "nonexistent": 50})

    def test_default_weights_uniform(self, db_with_segments):
        sched = Scheduler(db_with_segments, "g1")
        from spinlab.allocators import list_allocators
        n = len(list_allocators())
        assert len(sched.allocator.entries) == n

    def test_sync_picks_up_weight_change(self, db_with_segments):
        sched = Scheduler(db_with_segments, "g1")
        import json
        db_with_segments.save_allocator_config(
            "allocator_weights", json.dumps({"random": 100})
        )
        sched._sync_config_from_db()
        assert len(sched.allocator.entries) == 1
        alloc, weight = sched.allocator.entries[0]
        assert alloc.name == "random"
        assert weight == 100


class TestSchedulerRebuild:
    def test_rebuild_all_states(self, db_with_segments):
        sched = Scheduler(db_with_segments, "g1")
        segment_id = db_with_segments._test_segs[0].id
        sched.process_attempt(segment_id, time_ms=12000, completed=True)
        sched.process_attempt(segment_id, time_ms=11000, completed=True)
        sched.rebuild_all_states()
        rows = db_with_segments.load_all_model_states_for_segment(segment_id)
        assert len(rows) == len(list_estimators())


class TestOldConfigCleanup:
    def test_old_allocator_key_deleted_on_init(self, db_with_segments):
        db_with_segments.save_allocator_config("allocator", "greedy")
        Scheduler(db_with_segments, "g1")
        assert db_with_segments.load_allocator_config("allocator") is None


class TestStateFileFilter:
    def test_pick_next_skips_missing_state_files(self, tmp_path):
        db = Database(":memory:")
        db.upsert_game("g1", "Test", "any%")
        valid_state = tmp_path / "valid.mss"
        valid_state.write_bytes(b"\x00" * 100)
        # seg1 has a valid state file via waypoint
        seg1 = _make_seg_with_state(
            db, "g1", 1, "entrance", "checkpoint", valid_state,
            start_conds={"n": "1"},
        )
        # seg2's waypoint has a nonexistent path
        wp_start2 = Waypoint.make("g1", 2, "entrance", 0, {"n": "2"})
        wp_end2 = Waypoint.make("g1", 2, "checkpoint", 0, {"n": "2"})
        db.upsert_waypoint(wp_start2)
        db.upsert_waypoint(wp_end2)
        seg2 = Segment(
            id=Segment.make_id("g1", 2, "entrance", 0, "checkpoint", 0,
                               wp_start2.id, wp_end2.id),
            game_id="g1", level_number=2,
            start_type="entrance", start_ordinal=0,
            end_type="checkpoint", end_ordinal=0,
            start_waypoint_id=wp_start2.id, end_waypoint_id=wp_end2.id,
        )
        db.upsert_segment(seg2)
        db.add_save_state(WaypointSaveState(
            waypoint_id=wp_start2.id, variant_type="hot",
            state_path="/nonexistent/path.mss", is_default=True,
        ))
        sched = Scheduler(db, "g1")
        picked = sched.pick_next()
        assert picked is not None
        assert picked.segment_id == seg1.id


class TestSyncConfigFromDb:
    def test_allocator_weights_change_detected(self, db_with_segments):
        """Changing weights in the DB between pick_next calls should rebuild
        the allocator."""
        import json
        sched = Scheduler(db_with_segments, "g1")
        initial_weights_json = sched._weights_json

        new_weights = {"greedy": 100}
        db_with_segments.save_allocator_config(
            "allocator_weights", json.dumps(new_weights)
        )

        sched.pick_next()
        assert sched._weights_json != initial_weights_json
        assert json.loads(sched._weights_json) == new_weights

    def test_estimator_change_detected(self, db_with_segments):
        """Changing the estimator in the DB should update sched.estimator."""
        from spinlab.estimators import list_estimators
        sched = Scheduler(db_with_segments, "g1")
        initial_name = sched.estimator.name

        other = [n for n in list_estimators() if n != initial_name]
        if not other:
            pytest.skip("Only one estimator registered — can't test switch")
        new_name = other[0]

        db_with_segments.save_allocator_config("estimator", new_name)
        sched.pick_next()
        assert sched.estimator.name == new_name


class TestSetAllocatorWeights:
    def test_sum_must_equal_100(self, db_with_segments):
        sched = Scheduler(db_with_segments, "g1")
        with pytest.raises(ValueError, match="sum to 100"):
            sched.set_allocator_weights({"greedy": 50, "random": 30})

    def test_unknown_allocator_name_rejected(self, db_with_segments):
        sched = Scheduler(db_with_segments, "g1")
        with pytest.raises(ValueError, match="Unknown allocator"):
            sched.set_allocator_weights({"greedy": 50, "not_a_real_allocator": 50})

    def test_valid_weights_persisted(self, db_with_segments):
        import json
        sched = Scheduler(db_with_segments, "g1")
        sched.set_allocator_weights({"greedy": 60, "random": 40})
        raw = db_with_segments.load_allocator_config("allocator_weights")
        assert json.loads(raw) == {"greedy": 60, "random": 40}


class TestRebuildAllStates:
    def test_rebuilds_from_attempt_history(self, db_with_segments):
        """After recording some attempts, rebuild_all_states should
        produce model states for each segment with attempts."""
        sched = Scheduler(db_with_segments, "g1")

        segs = db_with_segments.get_all_segments_with_model("g1")
        assert segs, "fixture should provide segments"
        seg_id = segs[0]["id"]
        sched.process_attempt(seg_id, time_ms=5000, completed=True)

        sched.rebuild_all_states()

        row = db_with_segments.load_model_state(seg_id, sched.estimator.name)
        assert row is not None
        assert row["state_json"]
