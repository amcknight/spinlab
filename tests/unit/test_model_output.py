"""Tests for ModelOutput / Estimate dataclasses."""
import json

from spinlab.db import Database
from spinlab.models import (
    Attempt,
    Estimate,
    ModelOutput,
    Segment,
)


class TestEstimate:
    def test_round_trip_serialization(self):
        e = Estimate(expected_ms=12000.0, ms_per_attempt=150.0, floor_ms=7000.0)
        d = e.to_dict()
        e2 = Estimate.from_dict(d)
        assert e2.expected_ms == 12000.0
        assert e2.ms_per_attempt == 150.0
        assert e2.floor_ms == 7000.0

    def test_all_none(self):
        e = Estimate(expected_ms=None, ms_per_attempt=None, floor_ms=None)
        d = e.to_dict()
        e2 = Estimate.from_dict(d)
        assert e2.expected_ms is None
        assert e2.ms_per_attempt is None
        assert e2.floor_ms is None


class TestModelOutput:
    def test_round_trip_serialization(self):
        mo = ModelOutput(
            total=Estimate(expected_ms=12000.0, ms_per_attempt=150.0, floor_ms=9500.0),
            clean=Estimate(expected_ms=8000.0, ms_per_attempt=80.0, floor_ms=6200.0),
        )
        d = mo.to_dict()
        mo2 = ModelOutput.from_dict(d)
        assert mo2.total.expected_ms == 12000.0
        assert mo2.total.ms_per_attempt == 150.0
        assert mo2.total.floor_ms == 9500.0
        assert mo2.clean.expected_ms == 8000.0
        assert mo2.clean.ms_per_attempt == 80.0
        assert mo2.clean.floor_ms == 6200.0

    def test_nested_dict_structure(self):
        mo = ModelOutput(
            total=Estimate(expected_ms=1.0, ms_per_attempt=2.0, floor_ms=3.0),
            clean=Estimate(expected_ms=4.0, ms_per_attempt=5.0, floor_ms=6.0),
        )
        d = mo.to_dict()
        assert set(d.keys()) == {"total", "clean", "extras"}
        assert set(d["total"].keys()) == {"expected_ms", "ms_per_attempt", "floor_ms"}

    def test_all_none_sides(self):
        mo = ModelOutput(
            total=Estimate(expected_ms=None, ms_per_attempt=None, floor_ms=None),
            clean=Estimate(expected_ms=None, ms_per_attempt=None, floor_ms=None),
        )
        d = mo.to_dict()
        mo2 = ModelOutput.from_dict(d)
        assert mo2.total.expected_ms is None
        assert mo2.clean.expected_ms is None


class TestDBMultiModel:
    def _setup_db(self):
        db = Database(":memory:")
        db.upsert_game("g1", "Game", "any%")
        seg = Segment(
            id="s1", game_id="g1", level_number=1,
            start_type="entrance", start_ordinal=0,
            end_type="checkpoint", end_ordinal=0,
        )
        db.upsert_segment(seg)
        return db

    def test_save_and_load_multi_model_state(self):
        """The DB stores model_state rows keyed by (segment_id, estimator).
        Production now writes only `em_suite_sampler` rows, but the DB primitive
        still supports multiple estimator names per segment — this test pins that
        capability for future multi-model coexistence (e.g. Spec #3 PGM rollout).
        """
        db = self._setup_db()
        out_a = ModelOutput(
            total=Estimate(expected_ms=12000.0, ms_per_attempt=500.0, floor_ms=None),
            clean=Estimate(expected_ms=None, ms_per_attempt=None, floor_ms=None),
        )
        out_b = ModelOutput(
            total=Estimate(expected_ms=12500.0, ms_per_attempt=300.0, floor_ms=11000.0),
            clean=Estimate(expected_ms=12500.0, ms_per_attempt=300.0, floor_ms=11000.0),
        )
        db.save_model_state("s1", "em_suite_sampler", '{"mu": 12.0}', json.dumps(out_a.to_dict()))
        db.save_model_state("s1", "em_suite_alt", '{"n_completed": 5}', json.dumps(out_b.to_dict()))
        rows = db.load_all_model_states_for_segment("s1")
        assert len(rows) == 2
        names = {r["estimator"] for r in rows}
        assert names == {"em_suite_sampler", "em_suite_alt"}

    def test_load_model_state_by_estimator(self):
        db = self._setup_db()
        out = ModelOutput(
            total=Estimate(expected_ms=12000.0, ms_per_attempt=500.0, floor_ms=None),
            clean=Estimate(expected_ms=None, ms_per_attempt=None, floor_ms=None),
        )
        db.save_model_state("s1", "em_suite_sampler", '{"mu": 12.0}', json.dumps(out.to_dict()))
        row = db.load_model_state("s1", "em_suite_sampler")
        assert row is not None
        assert row["estimator"] == "em_suite_sampler"
        loaded_out = ModelOutput.from_dict(json.loads(row["output_json"]))
        assert loaded_out.total.expected_ms == 12000.0

    def test_attempt_with_deaths_and_clean_tail(self):
        db = self._setup_db()
        db.create_session("sess1", "g1")
        attempt = Attempt(
            segment_id="s1", session_id="sess1", completed=True,
            time_ms=12000, deaths=3, clean_tail_ms=4000,
        )
        db.log_attempt(attempt)
        rows = db.get_segment_attempts("s1")
        assert len(rows) == 1
        assert rows[0]["deaths"] == 3
        assert rows[0]["clean_tail_ms"] == 4000

    def test_attempt_defaults_zero_deaths(self):
        db = self._setup_db()
        db.create_session("sess1", "g1")
        attempt = Attempt(
            segment_id="s1", session_id="sess1", completed=True,
            time_ms=12000,
        )
        db.log_attempt(attempt)
        rows = db.get_segment_attempts("s1")
        assert rows[0]["deaths"] == 0
        # Post-Phase-0: clean_tail_ms reflects the wall-clock of the final
        # 'survived' event. With no deaths it equals time_ms by construction
        # (the closing event IS the entire episode). Scheduler.process_attempt
        # already coerced None→time_ms in this case, so estimator behavior
        # is unchanged.
        assert rows[0]["clean_tail_ms"] == 12000
