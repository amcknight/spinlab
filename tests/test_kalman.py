"""Tests for the Kalman estimator (new multi-model interface)."""
import pytest
from spinlab.estimators.kalman import KalmanEstimator, KalmanState
from spinlab.models import AttemptRecord, ModelOutput


def _attempt(time_ms: int | None, completed: bool, deaths: int = 0,
             clean_tail_ms: int | None = None) -> AttemptRecord:
    if clean_tail_ms is None and completed and time_ms is not None:
        clean_tail_ms = time_ms
    return AttemptRecord(
        time_ms=time_ms, completed=completed, deaths=deaths,
        clean_tail_ms=clean_tail_ms, created_at="2026-01-01T00:00:00",
    )


class TestKalmanProcessAttempt:
    def test_first_completed_attempt_initializes(self):
        est = KalmanEstimator()
        attempt = _attempt(12000, True)
        state = est.init_state(attempt, priors={})
        assert state.mu == pytest.approx(12.0)
        assert state.n_completed == 1
        assert state.n_attempts == 1

    def test_process_completed_updates_mu(self):
        est = KalmanEstimator()
        a1 = _attempt(12000, True)
        state = est.init_state(a1, priors={})
        a2 = _attempt(11000, True)
        state = est.process_attempt(state, a2, [a1, a2])
        assert state.n_completed == 2
        assert state.mu < 12.0

    def test_process_incomplete_increments_attempts_only(self):
        est = KalmanEstimator()
        a1 = _attempt(12000, True)
        state = est.init_state(a1, priors={})
        a2 = _attempt(None, False)
        state = est.process_attempt(state, a2, [a1, a2])
        assert state.n_completed == 1
        assert state.n_attempts == 2


class TestKalmanModelOutput:
    def test_produces_model_output(self):
        est = KalmanEstimator()
        a1 = _attempt(12000, True)
        state = est.init_state(a1, priors={})
        out = est.model_output(state, [a1])
        assert isinstance(out, ModelOutput)
        assert out.expected_time_ms == pytest.approx(12000.0)
        assert out.ms_per_attempt == pytest.approx(500.0)  # -d * 1000, default d=-0.5
        # Kalman floor = gold for now (placeholder)
        assert out.floor_estimate_ms == pytest.approx(12000.0)
        assert out.clean_floor_estimate_ms == pytest.approx(12000.0)

    def test_clean_expected_equals_expected(self):
        """Kalman doesn't distinguish clean/dirty yet."""
        est = KalmanEstimator()
        a1 = _attempt(12000, True)
        state = est.init_state(a1, priors={})
        out = est.model_output(state, [a1])
        assert out.clean_expected_ms == out.expected_time_ms

    def test_improving_attempts_positive_ms_per_attempt(self):
        est = KalmanEstimator()
        times = [12000, 11500, 11000, 10500, 10000, 9500, 9000, 8500, 8000, 7500]
        attempts = [_attempt(t, True) for t in times]
        state = est.init_state(attempts[0], priors={})
        for a in attempts[1:]:
            state = est.process_attempt(state, a, attempts)
        out = est.model_output(state, attempts)
        assert out.ms_per_attempt > 0

    def test_floor_equals_gold(self):
        """Kalman floor is gold_ms (placeholder for future uncertainty-based floor)."""
        est = KalmanEstimator()
        attempts = [_attempt(t, True) for t in [12000, 11000, 10000]]
        state = est.init_state(attempts[0], priors={})
        for a in attempts[1:]:
            state = est.process_attempt(state, a, attempts)
        gold_ms = min(a.time_ms for a in attempts) * 1.0
        out = est.model_output(state, attempts)
        assert out.floor_estimate_ms == pytest.approx(gold_ms)
        assert out.clean_floor_estimate_ms == pytest.approx(gold_ms)


class TestKalmanRebuildState:
    def test_rebuild_from_attempts(self):
        est = KalmanEstimator()
        attempts = [_attempt(12000, True), _attempt(None, False), _attempt(11000, True)]
        state = est.rebuild_state(attempts)
        assert state.n_completed == 2
        assert state.n_attempts == 3

    def test_rebuild_empty(self):
        est = KalmanEstimator()
        state = est.rebuild_state([])
        assert state.n_completed == 0
        assert state.n_attempts == 0


class TestKalmanDriftInfo:
    def test_drift_info_returns_dict(self):
        est = KalmanEstimator()
        a1 = _attempt(12000, True)
        state = est.init_state(a1, priors={})
        info = est.drift_info(state)
        assert "drift" in info
        assert "label" in info
        assert "ci_lower" in info
