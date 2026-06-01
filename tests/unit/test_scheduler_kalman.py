"""Tests for the scheduler coordinator."""
import json

import pytest

from spinlab.db import Database
from spinlab.models import Segment, Waypoint, WaypointSaveState
from spinlab.scheduler import Scheduler


def _make_seg_with_state(db, game_id, level, start_type, end_type,
                         state_path, start_conds=None, end_conds=None):
    """Create waypoints + segment + save state in the conventional variant
    (cold for entrance, hot for checkpoint); return segment."""
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
        description=f"Segment {level}",
        start_waypoint_id=wp_start.id, end_waypoint_id=wp_end.id,
    )
    db.upsert_segment(seg)
    variant = "cold" if start_type == "entrance" else "hot"
    db.add_save_state(WaypointSaveState(
        waypoint_id=wp_start.id, variant_type=variant,
        state_path=str(state_path),
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
        # Entrance segments resolve state from the 'cold' variant; use that
        # so the segments query returns the (nonexistent) path and pick_next
        # can exercise its "skip missing state files" branch.
        db.add_save_state(WaypointSaveState(
            waypoint_id=wp_start2.id, variant_type="cold",
            state_path="/nonexistent/path.mss",
        ))
        sched = Scheduler(db, "g1")
        picked = sched.pick_next()
        assert picked is not None
        assert picked.segment_id == seg1.id


class TestSchedulerRebuild:
    """Coverage for Scheduler.rebuild_all_states — production code reachable from
    finalize_run / save_and_finish_run and the /api/estimator-params route."""

    def test_rebuild_all_states_regenerates_em_suite_state(self, db_with_segments):
        """rebuild_all_states must rewrite the em_suite_sampler model_state row
        with correct counters even if the row was wiped beforehand."""
        sched = Scheduler(db_with_segments, "g1")
        seg_id = db_with_segments._test_segs[0].id

        # Seed two completed attempts so the state has real counters.
        sched.process_attempt(seg_id, time_ms=12000, completed=True, deaths=1)
        sched.process_attempt(seg_id, time_ms=11000, completed=True, deaths=1)

        # Corrupt the persisted state so we can prove rebuild_all_states
        # (not process_attempt) is what writes the correct result.
        db_with_segments.save_model_state(seg_id, "em_suite_sampler", "{}", "{}")

        # The corrupt row is now in place — rebuild must overwrite it.
        sched.rebuild_all_states()

        row = db_with_segments.load_model_state(seg_id, "em_suite_sampler")
        assert row is not None
        state = json.loads(row["state_json"])
        assert state["n_attempts"] == 2
        assert state["n_completed"] == 2

        # Only em_suite_sampler rows — no ghost estimators from a former
        # multi-estimator world.
        rows = db_with_segments.load_all_model_states_for_segment(seg_id)
        assert {r["estimator"] for r in rows} == {"em_suite_sampler"}


class TestSyncConfigFromDb:
    """Tests for _sync_config_from_db triggered via pick_next().

    The existing TestSchedulerWeights.test_sync_picks_up_weight_change tests
    _sync_config_from_db() directly. These tests verify the same mechanism
    fires automatically through pick_next(), and extend to estimator changes.
    """

    def test_allocator_weights_change_detected(self, db_with_segments):
        """Changing weights in the DB between pick_next calls should rebuild
        the allocator."""
        sched = Scheduler(db_with_segments, "g1")
        initial_weights = dict(sched.all_weights)

        new_weights = {"greedy": 100}
        db_with_segments.save_allocator_config(
            "allocator_weights", json.dumps(new_weights)
        )

        sched.pick_next()
        assert sched.all_weights != initial_weights
        assert sched.all_weights["greedy"] == 100

