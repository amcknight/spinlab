# tests/test_rolling_mean.py
"""Tests for Rolling Mean estimator."""
import pytest
from spinlab.estimators.rolling_mean import RollingMeanEstimator, RollingMeanState
from spinlab.models import AttemptRecord, Estimate, ModelOutput
from tests.factories import make_attempt_record, make_incomplete


class TestRollingMeanProcessAttempt:
    def test_init_from_first_attempt(self):
        est = RollingMeanEstimator()
        state = est.init_state(make_attempt_record(12000, True, clean_tail_ms=12000), priors={})
        assert state.n_completed == 1
        assert state.n_attempts == 1

    def test_process_multiple_attempts(self):
        est = RollingMeanEstimator()
        attempts = [make_attempt_record(t, True, clean_tail_ms=t) for t in [12000, 11500, 11000, 10500, 10000]]
        state = est.init_state(attempts[0], priors={})
        for a in attempts[1:]:
            state = est.process_attempt(state, a, attempts)
        assert state.n_completed == 5
        assert state.n_attempts == 5

    def test_incomplete_increments_attempts_only(self):
        est = RollingMeanEstimator()
        a1 = make_attempt_record(12000, True, clean_tail_ms=12000)
        state = est.init_state(a1, priors={})
        inc = make_incomplete()
        state = est.process_attempt(state, inc, [a1, inc])
        assert state.n_completed == 1
        assert state.n_attempts == 2


class TestRollingMeanModelOutput:
    def test_constant_times_zero_trend(self):
        est = RollingMeanEstimator()
        attempts = [make_attempt_record(10000, True, clean_tail_ms=10000) for _ in range(10)]
        state = est.init_state(attempts[0], priors={})
        for a in attempts[1:]:
            state = est.process_attempt(state, a, attempts)
        out = est.model_output(state, attempts)
        assert out.total.ms_per_attempt == pytest.approx(0.0)

    def test_strictly_decreasing_positive_trend(self):
        est = RollingMeanEstimator()
        times = [15000, 14000, 13000, 12000, 11000, 10000, 9000, 8000, 7000, 6000]
        attempts = [make_attempt_record(t, True, clean_tail_ms=t) for t in times]
        state = est.init_state(attempts[0], priors={})
        for a in attempts[1:]:
            state = est.process_attempt(state, a, attempts)
        out = est.model_output(state, attempts)
        assert out.total.ms_per_attempt > 0

    def test_strictly_increasing_negative_trend(self):
        est = RollingMeanEstimator()
        times = [6000, 7000, 8000, 9000, 10000, 11000, 12000, 13000, 14000, 15000]
        attempts = [make_attempt_record(t, True, clean_tail_ms=t) for t in times]
        state = est.init_state(attempts[0], priors={})
        for a in attempts[1:]:
            state = est.process_attempt(state, a, attempts)
        out = est.model_output(state, attempts)
        assert out.total.ms_per_attempt < 0

    def test_single_attempt_none_trend(self):
        est = RollingMeanEstimator()
        a1 = make_attempt_record(12000, True, clean_tail_ms=12000)
        state = est.init_state(a1, priors={})
        out = est.model_output(state, [a1])
        assert out.total.expected_ms == pytest.approx(12000.0)
        assert out.total.ms_per_attempt is None  # <2 attempts

    def test_two_attempts_computes_trend(self):
        est = RollingMeanEstimator()
        attempts = [
            make_attempt_record(12000, True, clean_tail_ms=12000),
            make_attempt_record(10000, True, clean_tail_ms=10000),
        ]
        state = est.init_state(attempts[0], priors={})
        state = est.process_attempt(state, attempts[1], attempts)
        out = est.model_output(state, attempts)
        assert out.total.ms_per_attempt > 0  # improving

    def test_floor_is_min_observed(self):
        est = RollingMeanEstimator()
        attempts = [make_attempt_record(t, True, clean_tail_ms=t) for t in [15000, 12000, 10000, 11000, 13000]]
        state = est.init_state(attempts[0], priors={})
        for a in attempts[1:]:
            state = est.process_attempt(state, a, attempts)
        out = est.model_output(state, attempts)
        assert out.total.floor_ms == pytest.approx(10000.0)
        assert out.clean.floor_ms == pytest.approx(10000.0)

    def test_dirty_attempts_separate_clean_and_total(self):
        est = RollingMeanEstimator()
        attempts = [
            make_attempt_record(20000, True, deaths=2, clean_tail_ms=8000),
            make_attempt_record(18000, True, deaths=1, clean_tail_ms=9000),
            make_attempt_record(15000, True, deaths=0, clean_tail_ms=15000),
            make_attempt_record(19000, True, deaths=2, clean_tail_ms=7000),
            make_attempt_record(14000, True, deaths=0, clean_tail_ms=14000),
        ]
        state = est.init_state(attempts[0], priors={})
        for a in attempts[1:]:
            state = est.process_attempt(state, a, attempts)
        out = est.model_output(state, attempts)
        assert out.clean.expected_ms != out.total.expected_ms
        assert out.clean.floor_ms == pytest.approx(7000.0)
        assert out.total.floor_ms == pytest.approx(14000.0)

    def test_no_clean_data_returns_none_clean(self):
        """If no clean_tail_ms values exist, clean side is all None."""
        est = RollingMeanEstimator()
        attempts = [
            AttemptRecord(time_ms=12000, completed=True, deaths=0, clean_tail_ms=None, created_at="2026-01-01T00:00:00"),
            AttemptRecord(time_ms=11000, completed=True, deaths=0, clean_tail_ms=None, created_at="2026-01-01T00:00:00"),
        ]
        state = est.init_state(attempts[0], priors={})
        state = est.process_attempt(state, attempts[1], attempts)
        out = est.model_output(state, attempts)
        assert out.clean.expected_ms is None
        assert out.clean.ms_per_attempt is None
        assert out.clean.floor_ms is None


class TestRollingMeanRebuild:
    def test_rebuild_from_attempts(self):
        est = RollingMeanEstimator()
        attempts = [
            make_attempt_record(12000, True, clean_tail_ms=12000),
            make_incomplete(),
            make_attempt_record(11000, True, clean_tail_ms=11000),
        ]
        state = est.rebuild_state(attempts)
        assert state.n_completed == 2
        assert state.n_attempts == 3

    def test_rebuild_empty(self):
        est = RollingMeanEstimator()
        state = est.rebuild_state([])
        assert state.n_completed == 0
        assert state.n_attempts == 0
