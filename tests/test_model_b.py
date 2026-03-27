# tests/test_model_b.py
"""Tests for Model B (exponential decay estimator)."""
import math
import pytest
from spinlab.estimators.model_b import ModelBEstimator, ModelBState
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


def _synthetic_exp_attempts(
    n: int = 25, amplitude: float = 12000.0, decay_rate: float = 0.1,
    asymptote: float = 3000.0,
) -> list[AttemptRecord]:
    """Generate n attempts following exact a*exp(-b*n)+c (no noise)."""
    return [
        _attempt(int(amplitude * math.exp(-decay_rate * i) + asymptote))
        for i in range(n)
    ]


class TestModelBProcessAttempt:
    def test_init_from_first_attempt(self):
        est = ModelBEstimator()
        state = est.init_state(_attempt(12000), priors={})
        assert state.n_completed == 1
        assert state.n_attempts == 1

    def test_process_tracks_counts(self):
        est = ModelBEstimator()
        attempts = _synthetic_exp_attempts(5)
        state = est.init_state(attempts[0], priors={})
        for a in attempts[1:]:
            state = est.process_attempt(state, a, attempts)
        assert state.n_completed == 5
        assert state.n_attempts == 5

    def test_incomplete_increments_attempts_only(self):
        est = ModelBEstimator()
        state = est.init_state(_attempt(12000), priors={})
        state = est.process_attempt(state, _incomplete(), [_attempt(12000), _incomplete()])
        assert state.n_completed == 1
        assert state.n_attempts == 2


class TestModelBFit:
    def test_recovers_known_asymptote(self):
        """Fit on exact exponential data should recover the asymptote."""
        est = ModelBEstimator()
        attempts = _synthetic_exp_attempts(25, amplitude=12000, decay_rate=0.1, asymptote=3000)
        state = est.init_state(attempts[0], priors={})
        for a in attempts[1:]:
            state = est.process_attempt(state, a, attempts)
        # asymptote should be close to 3000
        assert state.asymptote == pytest.approx(3000, rel=0.05)

    def test_recovers_known_decay_rate(self):
        est = ModelBEstimator()
        attempts = _synthetic_exp_attempts(25, amplitude=12000, decay_rate=0.1, asymptote=3000)
        state = est.init_state(attempts[0], priors={})
        for a in attempts[1:]:
            state = est.process_attempt(state, a, attempts)
        assert state.decay_rate == pytest.approx(0.1, rel=0.1)


class TestModelBModelOutput:
    def test_output_with_enough_data(self):
        est = ModelBEstimator()
        attempts = _synthetic_exp_attempts(25)
        state = est.init_state(attempts[0], priors={})
        for a in attempts[1:]:
            state = est.process_attempt(state, a, attempts)
        out = est.model_output(state, attempts)
        assert isinstance(out, ModelOutput)
        assert out.ms_per_attempt > 0
        assert out.floor_estimate_ms > 0
        assert out.clean_floor_estimate_ms > 0
        assert out.clean_floor_estimate_ms < out.expected_time_ms

    def test_ms_per_attempt_matches_derivative(self):
        """ms_per_attempt should approximate a*b*exp(-b*(n-1)) for synthetic data."""
        est = ModelBEstimator()
        a, b, c = 12000.0, 0.1, 3000.0
        attempts = _synthetic_exp_attempts(25, amplitude=a, decay_rate=b, asymptote=c)
        state = est.init_state(attempts[0], priors={})
        for att in attempts[1:]:
            state = est.process_attempt(state, att, attempts)
        out = est.model_output(state, attempts)
        expected_deriv = a * b * math.exp(-b * 24)  # derivative at n=24
        assert out.ms_per_attempt == pytest.approx(expected_deriv, rel=0.15)

    def test_floor_never_negative(self):
        est = ModelBEstimator()
        attempts = _synthetic_exp_attempts(25, asymptote=100)
        state = est.init_state(attempts[0], priors={})
        for a in attempts[1:]:
            state = est.process_attempt(state, a, attempts)
        out = est.model_output(state, attempts)
        assert out.floor_estimate_ms >= 0
        assert out.clean_floor_estimate_ms >= 0

    def test_fallback_with_few_points(self):
        """With <3 completed, falls back to simple stats."""
        est = ModelBEstimator()
        attempts = [_attempt(12000), _attempt(11500)]
        state = est.init_state(attempts[0], priors={})
        state = est.process_attempt(state, attempts[1], attempts)
        out = est.model_output(state, attempts)
        assert isinstance(out, ModelOutput)
        assert out.floor_estimate_ms > 0

    def test_two_fits_total_and_clean(self):
        """With dirty attempts, total and clean floors should differ."""
        est = ModelBEstimator()
        n = 25
        attempts = []
        for i in range(n):
            total = int(12000 * math.exp(-0.1 * i) + 5000)
            clean = int(8000 * math.exp(-0.1 * i) + 3000)
            deaths = 2 if i % 3 == 0 else 0
            attempts.append(_attempt(total, deaths=deaths, clean_tail_ms=clean))
        state = est.init_state(attempts[0], priors={})
        for a in attempts[1:]:
            state = est.process_attempt(state, a, attempts)
        out = est.model_output(state, attempts)
        assert out.floor_estimate_ms > out.clean_floor_estimate_ms


class TestModelBRebuild:
    def test_rebuild_from_attempts(self):
        est = ModelBEstimator()
        attempts = [_attempt(12000), _incomplete(), _attempt(11000)]
        state = est.rebuild_state(attempts)
        assert state.n_completed == 2
        assert state.n_attempts == 3
