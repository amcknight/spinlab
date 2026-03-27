# tests/test_model_a.py
"""Tests for Model A (rolling statistics estimator)."""
import pytest
from spinlab.estimators.model_a import ModelAEstimator, ModelAState
from spinlab.models import AttemptRecord, ModelOutput


def _attempt(time_ms: int, deaths: int = 0, clean_tail_ms: int | None = None) -> AttemptRecord:
    if clean_tail_ms is None:
        clean_tail_ms = time_ms
    return AttemptRecord(
        time_ms=time_ms, completed=True, deaths=deaths,
        clean_tail_ms=clean_tail_ms, created_at="2026-01-01T00:00:00",
    )


def _incomplete() -> AttemptRecord:
    return AttemptRecord(
        time_ms=None, completed=False, deaths=0,
        clean_tail_ms=None, created_at="2026-01-01T00:00:00",
    )


class TestModelAProcessAttempt:
    def test_init_from_first_attempt(self):
        est = ModelAEstimator()
        state = est.init_state(_attempt(12000), priors={})
        assert state.n_completed == 1
        assert state.n_attempts == 1

    def test_process_multiple_attempts(self):
        est = ModelAEstimator()
        attempts = [_attempt(t) for t in [12000, 11500, 11000, 10500, 10000]]
        state = est.init_state(attempts[0], priors={})
        for a in attempts[1:]:
            state = est.process_attempt(state, a, attempts)
        assert state.n_completed == 5
        assert state.n_attempts == 5

    def test_incomplete_increments_attempts_only(self):
        est = ModelAEstimator()
        state = est.init_state(_attempt(12000), priors={})
        state = est.process_attempt(state, _incomplete(), [_attempt(12000), _incomplete()])
        assert state.n_completed == 1
        assert state.n_attempts == 2


class TestModelAModelOutput:
    def test_constant_times_zero_trend(self):
        est = ModelAEstimator()
        attempts = [_attempt(10000) for _ in range(10)]
        state = est.init_state(attempts[0], priors={})
        for a in attempts[1:]:
            state = est.process_attempt(state, a, attempts)
        out = est.model_output(state, attempts)
        assert out.ms_per_attempt == pytest.approx(0.0)

    def test_strictly_decreasing_positive_trend(self):
        est = ModelAEstimator()
        times = [15000, 14000, 13000, 12000, 11000, 10000, 9000, 8000, 7000, 6000]
        attempts = [_attempt(t) for t in times]
        state = est.init_state(attempts[0], priors={})
        for a in attempts[1:]:
            state = est.process_attempt(state, a, attempts)
        out = est.model_output(state, attempts)
        assert out.ms_per_attempt > 0

    def test_strictly_increasing_negative_trend(self):
        est = ModelAEstimator()
        times = [6000, 7000, 8000, 9000, 10000, 11000, 12000, 13000, 14000, 15000]
        attempts = [_attempt(t) for t in times]
        state = est.init_state(attempts[0], priors={})
        for a in attempts[1:]:
            state = est.process_attempt(state, a, attempts)
        out = est.model_output(state, attempts)
        assert out.ms_per_attempt < 0

    def test_single_attempt_zero_trend(self):
        est = ModelAEstimator()
        a1 = _attempt(12000)
        state = est.init_state(a1, priors={})
        out = est.model_output(state, [a1])
        assert out.expected_time_ms == pytest.approx(12000.0)
        assert out.ms_per_attempt == pytest.approx(0.0)

    def test_floor_is_min_observed(self):
        est = ModelAEstimator()
        attempts = [_attempt(t) for t in [15000, 12000, 10000, 11000, 13000]]
        state = est.init_state(attempts[0], priors={})
        for a in attempts[1:]:
            state = est.process_attempt(state, a, attempts)
        out = est.model_output(state, attempts)
        assert out.floor_estimate_ms == pytest.approx(10000.0)
        assert out.clean_floor_estimate_ms == pytest.approx(10000.0)

    def test_dirty_attempts_separate_clean_and_total(self):
        """clean_expected and expected_time differ when deaths are present."""
        est = ModelAEstimator()
        attempts = [
            _attempt(20000, deaths=2, clean_tail_ms=8000),
            _attempt(18000, deaths=1, clean_tail_ms=9000),
            _attempt(15000, deaths=0, clean_tail_ms=15000),
            _attempt(19000, deaths=2, clean_tail_ms=7000),
            _attempt(14000, deaths=0, clean_tail_ms=14000),
        ]
        state = est.init_state(attempts[0], priors={})
        for a in attempts[1:]:
            state = est.process_attempt(state, a, attempts)
        out = est.model_output(state, attempts)
        assert out.clean_expected_ms != out.expected_time_ms
        assert out.clean_floor_estimate_ms == pytest.approx(7000.0)
        assert out.floor_estimate_ms == pytest.approx(14000.0)


class TestModelARebuild:
    def test_rebuild_from_attempts(self):
        est = ModelAEstimator()
        attempts = [_attempt(12000), _incomplete(), _attempt(11000)]
        state = est.rebuild_state(attempts)
        assert state.n_completed == 2
        assert state.n_attempts == 3

    def test_rebuild_empty(self):
        est = ModelAEstimator()
        state = est.rebuild_state([])
        assert state.n_completed == 0
        assert state.n_attempts == 0
