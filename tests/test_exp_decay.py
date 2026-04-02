# tests/test_exp_decay.py
"""Tests for Exp Decay estimator."""
import math
import pytest

np = pytest.importorskip("numpy")
from spinlab.estimators.exp_decay import ExpDecayEstimator, ExpDecayState
from spinlab.models import AttemptRecord, Estimate, ModelOutput


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


class TestExpDecayProcessAttempt:
    def test_init_from_first_attempt(self):
        est = ExpDecayEstimator()
        state = est.init_state(_attempt(12000), priors={})
        assert state.n_completed == 1
        assert state.n_attempts == 1

    def test_process_tracks_counts(self):
        est = ExpDecayEstimator()
        attempts = _synthetic_exp_attempts(5)
        state = est.init_state(attempts[0], priors={})
        for a in attempts[1:]:
            state = est.process_attempt(state, a, attempts)
        assert state.n_completed == 5
        assert state.n_attempts == 5

    def test_incomplete_increments_attempts_only(self):
        est = ExpDecayEstimator()
        state = est.init_state(_attempt(12000), priors={})
        state = est.process_attempt(state, _incomplete(), [_attempt(12000), _incomplete()])
        assert state.n_completed == 1
        assert state.n_attempts == 2


class TestExpDecayFit:
    def test_recovers_known_asymptote(self):
        """Fit on exact exponential data should recover the asymptote."""
        est = ExpDecayEstimator()
        attempts = _synthetic_exp_attempts(25, amplitude=12000, decay_rate=0.1, asymptote=3000)
        state = est.init_state(attempts[0], priors={})
        for a in attempts[1:]:
            state = est.process_attempt(state, a, attempts)
        assert state.asymptote == pytest.approx(3000, rel=0.05)

    def test_recovers_known_decay_rate(self):
        est = ExpDecayEstimator()
        attempts = _synthetic_exp_attempts(25, amplitude=12000, decay_rate=0.1, asymptote=3000)
        state = est.init_state(attempts[0], priors={})
        for a in attempts[1:]:
            state = est.process_attempt(state, a, attempts)
        assert state.decay_rate == pytest.approx(0.1, rel=0.1)


class TestExpDecayModelOutput:
    def test_output_with_enough_data(self):
        est = ExpDecayEstimator()
        attempts = _synthetic_exp_attempts(25)
        state = est.init_state(attempts[0], priors={})
        for a in attempts[1:]:
            state = est.process_attempt(state, a, attempts)
        out = est.model_output(state, attempts)
        assert isinstance(out, ModelOutput)
        assert out.total.ms_per_attempt > 0
        assert out.total.floor_ms > 0
        assert out.clean.floor_ms > 0
        assert out.clean.floor_ms < out.total.expected_ms

    def test_ms_per_attempt_is_discrete_difference(self):
        """ms_per_attempt should be f(n) - f(n+1) from total fit."""
        est = ExpDecayEstimator()
        a, b, c = 12000.0, 0.1, 3000.0
        attempts = _synthetic_exp_attempts(25, amplitude=a, decay_rate=b, asymptote=c)
        state = est.init_state(attempts[0], priors={})
        for att in attempts[1:]:
            state = est.process_attempt(state, att, attempts)
        out = est.model_output(state, attempts)
        # Discrete difference at n=25: f(25) - f(26)
        f_n = a * math.exp(-b * 25) + c
        f_n1 = a * math.exp(-b * 26) + c
        expected_mpa = f_n - f_n1
        assert out.total.ms_per_attempt == pytest.approx(expected_mpa, rel=0.15)

    def test_floor_never_negative(self):
        est = ExpDecayEstimator()
        attempts = _synthetic_exp_attempts(25, asymptote=100)
        state = est.init_state(attempts[0], priors={})
        for a in attempts[1:]:
            state = est.process_attempt(state, a, attempts)
        out = est.model_output(state, attempts)
        assert out.total.floor_ms >= 0
        assert out.clean.floor_ms >= 0

    def test_few_points_returns_none(self):
        """With <3 completed, returns all None — no silent fallback."""
        est = ExpDecayEstimator()
        attempts = [_attempt(12000), _attempt(11500)]
        state = est.init_state(attempts[0], priors={})
        state = est.process_attempt(state, attempts[1], attempts)
        out = est.model_output(state, attempts)
        assert out.total.expected_ms is None
        assert out.total.ms_per_attempt is None
        assert out.total.floor_ms is None
        assert out.clean.expected_ms is None

    def test_two_fits_total_and_clean(self):
        est = ExpDecayEstimator()
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
        assert out.total.floor_ms > out.clean.floor_ms


class TestExpDecayRebuild:
    def test_rebuild_from_attempts(self):
        est = ExpDecayEstimator()
        attempts = [_attempt(12000), _incomplete(), _attempt(11000)]
        state = est.rebuild_state(attempts)
        assert state.n_completed == 2
        assert state.n_attempts == 3
