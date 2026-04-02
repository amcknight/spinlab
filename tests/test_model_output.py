"""Tests for AttemptRecord and ModelOutput dataclasses."""
from spinlab.models import AttemptRecord, Estimate, ModelOutput


class TestAttemptRecord:
    def test_completed_attempt(self):
        ar = AttemptRecord(
            time_ms=12000, completed=True, deaths=2,
            clean_tail_ms=4500, created_at="2026-03-27T12:00:00",
        )
        assert ar.time_ms == 12000
        assert ar.completed is True
        assert ar.deaths == 2
        assert ar.clean_tail_ms == 4500

    def test_incomplete_attempt(self):
        ar = AttemptRecord(
            time_ms=None, completed=False, deaths=0,
            clean_tail_ms=None, created_at="2026-03-27T12:00:00",
        )
        assert ar.time_ms is None
        assert ar.completed is False
        assert ar.clean_tail_ms is None

    def test_zero_death_clean_tail_equals_time(self):
        ar = AttemptRecord(
            time_ms=8000, completed=True, deaths=0,
            clean_tail_ms=8000, created_at="2026-03-27T12:00:00",
        )
        assert ar.clean_tail_ms == ar.time_ms


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
        assert set(d.keys()) == {"total", "clean"}
        assert set(d["total"].keys()) == {"expected_ms", "ms_per_attempt", "floor_ms"}

    def test_v1_backward_compat(self):
        """V1 flat dict should load into total side, clean gets all None."""
        v1 = {
            "expected_time_ms": 12000.0, "clean_expected_ms": 8000.0,
            "ms_per_attempt": 150.0, "floor_estimate_ms": 7000.0,
            "clean_floor_estimate_ms": 6000.0,
        }
        mo = ModelOutput.from_dict(v1)
        assert mo.total.expected_ms == 12000.0
        assert mo.total.ms_per_attempt == 150.0
        assert mo.total.floor_ms == 7000.0
        assert mo.clean.expected_ms == 8000.0
        assert mo.clean.ms_per_attempt is None
        assert mo.clean.floor_ms == 6000.0

    def test_all_none_sides(self):
        mo = ModelOutput(
            total=Estimate(expected_ms=None, ms_per_attempt=None, floor_ms=None),
            clean=Estimate(expected_ms=None, ms_per_attempt=None, floor_ms=None),
        )
        d = mo.to_dict()
        mo2 = ModelOutput.from_dict(d)
        assert mo2.total.expected_ms is None
        assert mo2.clean.expected_ms is None


import json
from spinlab.db import Database
from spinlab.models import Attempt, AttemptRecord, Estimate, ModelOutput, Segment


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
        db = self._setup_db()
        out_k = ModelOutput(
            total=Estimate(expected_ms=12000.0, ms_per_attempt=500.0, floor_ms=None),
            clean=Estimate(expected_ms=None, ms_per_attempt=None, floor_ms=None),
        )
        out_r = ModelOutput(
            total=Estimate(expected_ms=12500.0, ms_per_attempt=300.0, floor_ms=11000.0),
            clean=Estimate(expected_ms=12500.0, ms_per_attempt=300.0, floor_ms=11000.0),
        )
        db.save_model_state("s1", "kalman", '{"mu": 12.0}', json.dumps(out_k.to_dict()))
        db.save_model_state("s1", "rolling_mean", '{"n_completed": 5}', json.dumps(out_r.to_dict()))
        rows = db.load_all_model_states_for_segment("s1")
        assert len(rows) == 2
        names = {r["estimator"] for r in rows}
        assert names == {"kalman", "rolling_mean"}

    def test_load_model_state_by_estimator(self):
        db = self._setup_db()
        out = ModelOutput(
            total=Estimate(expected_ms=12000.0, ms_per_attempt=500.0, floor_ms=None),
            clean=Estimate(expected_ms=None, ms_per_attempt=None, floor_ms=None),
        )
        db.save_model_state("s1", "kalman", '{"mu": 12.0}', json.dumps(out.to_dict()))
        row = db.load_model_state("s1", "kalman")
        assert row is not None
        assert row["estimator"] == "kalman"
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
        assert rows[0]["clean_tail_ms"] is None
