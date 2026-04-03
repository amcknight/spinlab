"""Tests for the Kalman estimator (new multi-model interface)."""
import pytest
from spinlab.estimators.kalman import KalmanEstimator, KalmanState
from spinlab.models import AttemptRecord, Estimate, ModelOutput
from tests.factories import make_attempt_record, make_incomplete


class TestKalmanProcessAttempt:
    def test_first_completed_attempt_initializes(self):
        est = KalmanEstimator()
        attempt = make_attempt_record(12000, True)
        state = est.init_state(attempt, priors={})
        out = est.model_output(state, [attempt])
        assert out.total.expected_ms == pytest.approx(12000.0)
        assert state.n_completed == 1
        assert state.n_attempts == 1

    def test_process_completed_updates_mu(self):
        est = KalmanEstimator()
        a1 = make_attempt_record(12000, True)
        state = est.init_state(a1, priors={})
        a2 = make_attempt_record(11000, True)
        state = est.process_attempt(state, a2, [a1, a2])
        out = est.model_output(state, [a1, a2])
        assert state.n_completed == 2
        assert out.total.expected_ms < 12000.0

    def test_process_incomplete_increments_attempts_only(self):
        est = KalmanEstimator()
        a1 = make_attempt_record(12000, True)
        state = est.init_state(a1, priors={})
        a2 = make_incomplete()
        state = est.process_attempt(state, a2, [a1, a2])
        assert state.n_completed == 1
        assert state.n_attempts == 2


class TestKalmanModelOutput:
    def test_produces_model_output(self):
        est = KalmanEstimator()
        a1 = make_attempt_record(12000, True)
        state = est.init_state(a1, priors={})
        out = est.model_output(state, [a1])
        assert isinstance(out, ModelOutput)
        # expected = (mu + d) * 1000 = (12.0 + 0.0) * 1000 = 12000
        assert out.total.expected_ms == pytest.approx(12000.0)
        assert out.total.ms_per_attempt == pytest.approx(0.0)  # -d * 1000
        assert out.total.floor_ms is None

    def test_clean_side_tracks_clean_tail(self):
        """Clean filter should track clean_tail_ms when available."""
        est = KalmanEstimator()
        a1 = make_attempt_record(15000, True, deaths=2, clean_tail_ms=8000)
        state = est.init_state(a1, priors={})
        out = est.model_output(state, [a1])
        # clean_tail_ms = 8000 → 8.0s → expected 8000ms
        assert out.clean.expected_ms == pytest.approx(8000.0)
        assert out.clean.ms_per_attempt is not None

    def test_clean_side_none_when_no_clean_data(self):
        """If no clean_tail_ms data exists, clean should be all None."""
        est = KalmanEstimator()
        a1 = make_attempt_record(12000, True, deaths=0, clean_tail_ms=None)
        state = est.init_state(AttemptRecord(
            time_ms=12000, completed=True, deaths=0,
            clean_tail_ms=None, created_at="2026-01-01T00:00:00",
        ), priors={})
        out = est.model_output(state, [a1])
        assert out.clean.expected_ms is None
        assert out.clean.ms_per_attempt is None
        assert out.clean.floor_ms is None

    def test_clean_filter_updates_with_new_data(self):
        """Clean filter should update when new clean_tail_ms data arrives."""
        est = KalmanEstimator()
        a1 = make_attempt_record(15000, True, deaths=2, clean_tail_ms=8000)
        state = est.init_state(a1, priors={})
        a2 = make_attempt_record(14000, True, deaths=1, clean_tail_ms=7000)
        state = est.process_attempt(state, a2, [a1, a2])
        out = est.model_output(state, [a1, a2])
        # Should have moved toward 7.0s from initial 8.0s
        assert out.clean.expected_ms is not None
        assert out.clean.expected_ms < 8000.0

    def test_clean_filter_ignores_no_clean_tail(self):
        """Attempts without clean_tail_ms should not update clean filter."""
        est = KalmanEstimator()
        a1 = make_attempt_record(15000, True, deaths=2, clean_tail_ms=8000)
        state = est.init_state(a1, priors={})
        # Incomplete attempt — no clean tail
        a2 = make_incomplete()
        state = est.process_attempt(state, a2, [a1, a2])
        assert state.c_n_completed == 1  # unchanged

    def test_clean_filter_skips_zero_death_attempts(self):
        """Zero-death attempts have clean_tail == total; still should update clean."""
        est = KalmanEstimator()
        # First attempt has deaths + clean tail
        a1 = make_attempt_record(15000, True, deaths=2, clean_tail_ms=8000)
        state = est.init_state(a1, priors={})
        # Second attempt: 0 deaths, clean_tail_ms == time_ms
        a2 = make_attempt_record(9000, True, deaths=0, clean_tail_ms=9000)
        state = est.process_attempt(state, a2, [a1, a2])
        assert state.c_n_completed == 2

    def test_improving_attempts_positive_ms_per_attempt(self):
        est = KalmanEstimator()
        times = [12000, 11500, 11000, 10500, 10000, 9500, 9000, 8500, 8000, 7500]
        attempts = [make_attempt_record(t, True) for t in times]
        state = est.init_state(attempts[0], priors={})
        for a in attempts[1:]:
            state = est.process_attempt(state, a, attempts)
        out = est.model_output(state, attempts)
        assert out.total.ms_per_attempt > 0

    def test_expected_predicts_forward(self):
        """expected_ms should be mu + d (predicted next), not just mu (current)."""
        est = KalmanEstimator()
        # Feed a consistently improving sequence so drift (d) becomes negative
        times = [12000, 11000, 10000, 9000, 8000]
        attempts = [make_attempt_record(t, True) for t in times]
        state = est.init_state(attempts[0], priors={})
        for a in attempts[1:]:
            state = est.process_attempt(state, a, attempts)
        out = est.model_output(state, attempts)
        # With negative drift, expected_ms (= (mu + d) * 1000) should be less than mu * 1000
        assert out.total.expected_ms < state.mu * 1000


class TestKalmanRebuildState:
    def test_rebuild_from_attempts(self):
        est = KalmanEstimator()
        attempts = [make_attempt_record(12000, True), make_incomplete(), make_attempt_record(11000, True)]
        state = est.rebuild_state(attempts)
        assert state.n_completed == 2
        assert state.n_attempts == 3

    def test_rebuild_includes_clean_state(self):
        est = KalmanEstimator()
        attempts = [
            make_attempt_record(15000, True, deaths=2, clean_tail_ms=8000),
            make_incomplete(),
            make_attempt_record(14000, True, deaths=1, clean_tail_ms=7000),
        ]
        state = est.rebuild_state(attempts)
        assert state.c_n_completed == 2
        assert state.c_mu != 0.0  # should have been initialized

    def test_rebuild_empty(self):
        est = KalmanEstimator()
        state = est.rebuild_state([])
        assert state.n_completed == 0
        assert state.n_attempts == 0


class TestKalmanDriftInfo:
    def test_drift_info_returns_dict(self):
        est = KalmanEstimator()
        a1 = make_attempt_record(12000, True)
        state = est.init_state(a1, priors={})
        info = est.drift_info(state)
        assert "drift" in info
        assert "label" in info
        assert "ci_lower" in info


class TestKalmanGetPriors:
    def test_no_mature_states_returns_defaults(self, tmp_path):
        from spinlab.db import Database
        db = Database(str(tmp_path / "test.db"))
        db.upsert_game("g1", "Game", "any%")
        est = KalmanEstimator()
        priors = est.get_priors(db, "g1")
        assert priors["d"] == 0.0
        assert priors["R"] == 25.0

    def test_population_priors_from_mature_states(self, tmp_path):
        import json
        from spinlab.db import Database
        from spinlab.models import Segment
        db = Database(str(tmp_path / "test.db"))
        db.upsert_game("g1", "Game", "any%")
        # Create two segments with mature kalman states (n_completed >= 10)
        for i in range(2):
            seg = Segment(
                id=f"s{i}", game_id="g1", level_number=i,
                start_type="entrance", start_ordinal=0,
                end_type="checkpoint", end_ordinal=0,
            )
            db.upsert_segment(seg)
            state = KalmanState(
                mu=10.0 + i, d=-0.3 - (0.1 * i), R=20.0 + i,
                Q_mm=0.2, Q_dd=0.02,
                n_completed=15, n_attempts=20,
            )
            db.save_model_state(f"s{i}", "kalman", json.dumps(state.to_dict()), "{}")
        est = KalmanEstimator()
        priors = est.get_priors(db, "g1")
        # Should be averages of the two states
        assert priors["d"] == pytest.approx((-0.3 + -0.4) / 2)
        assert priors["R"] == pytest.approx((20.0 + 21.0) / 2)
