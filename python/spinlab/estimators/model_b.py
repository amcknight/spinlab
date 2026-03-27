# python/spinlab/estimators/model_b.py
"""Model B: Exponential decay estimator.

Fits time(n) = amplitude * exp(-decay_rate * n) + asymptote
via scipy.optimize.curve_fit. Two fits: one on total times, one on clean tails.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass

import numpy as np
from scipy.optimize import curve_fit

from spinlab.estimators import Estimator, EstimatorState, register_estimator
from spinlab.models import AttemptRecord, ModelOutput

MIN_POINTS_FOR_FIT = 3


def _exp_decay(n: np.ndarray, amplitude: float, decay_rate: float, asymptote: float) -> np.ndarray:
    return amplitude * np.exp(-decay_rate * n) + asymptote


def _fit_exp_decay(ns: np.ndarray, ts: np.ndarray) -> tuple[float, float, float, float]:
    """Fit amplitude*exp(-decay_rate*n)+asymptote. Returns (amplitude, decay_rate, asymptote, sigma)."""
    c0 = float(np.min(ts)) * 0.95
    a0 = max(float(np.median(ts[:5])) - c0, 100.0)
    b0 = 0.03
    try:
        popt, _ = curve_fit(
            _exp_decay, ns, ts,
            p0=[a0, b0, c0],
            bounds=([0, 1e-6, 0], [np.inf, 2.0, float(np.min(ts))]),
            maxfev=5000,
        )
        amplitude, decay_rate, asymptote = popt
        residuals = ts - _exp_decay(ns, amplitude, decay_rate, asymptote)
        sigma = float(np.std(residuals))
        return float(amplitude), float(decay_rate), float(asymptote), sigma
    except RuntimeError:
        asymptote = float(np.min(ts)) * 0.95
        amplitude = max(float(np.median(ts[:5])) - asymptote, 100.0)
        decay_rate = 0.01
        sigma = float(np.std(ts))
        return amplitude, decay_rate, asymptote, sigma


@dataclass
class ModelBState(EstimatorState):
    """Bookkeeping + cached fit params. Model B recomputes curve_fit each time."""

    n_completed: int = 0
    n_attempts: int = 0
    # Cached fit params for clean tail fit
    amplitude: float = 0.0
    decay_rate: float = 0.0
    asymptote: float = 0.0
    sigma: float = 0.0
    # Cached fit params for total time fit
    total_amplitude: float = 0.0
    total_decay_rate: float = 0.0
    total_asymptote: float = 0.0
    total_sigma: float = 0.0

    def to_dict(self) -> dict:
        return {
            "n_completed": self.n_completed, "n_attempts": self.n_attempts,
            "amplitude": self.amplitude, "decay_rate": self.decay_rate,
            "asymptote": self.asymptote, "sigma": self.sigma,
            "total_amplitude": self.total_amplitude,
            "total_decay_rate": self.total_decay_rate,
            "total_asymptote": self.total_asymptote,
            "total_sigma": self.total_sigma,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ModelBState":
        return cls(
            n_completed=d.get("n_completed", 0), n_attempts=d.get("n_attempts", 0),
            amplitude=d.get("amplitude", 0.0), decay_rate=d.get("decay_rate", 0.0),
            asymptote=d.get("asymptote", 0.0), sigma=d.get("sigma", 0.0),
            total_amplitude=d.get("total_amplitude", 0.0),
            total_decay_rate=d.get("total_decay_rate", 0.0),
            total_asymptote=d.get("total_asymptote", 0.0),
            total_sigma=d.get("total_sigma", 0.0),
        )


@register_estimator
class ModelBEstimator(Estimator):
    name = "model_b"

    def _run_fits(self, completed: list[AttemptRecord]) -> ModelBState:
        """Run both fits (clean tails and total times) and return updated state."""
        state = ModelBState(n_completed=len(completed), n_attempts=len(completed))
        if len(completed) < MIN_POINTS_FOR_FIT:
            return state

        ns = np.arange(len(completed), dtype=float)

        # Fit on clean tails
        clean_ts = np.array([a.clean_tail_ms for a in completed], dtype=float)
        a, b, c, sigma = _fit_exp_decay(ns, clean_ts)
        state.amplitude = a
        state.decay_rate = b
        state.asymptote = c
        state.sigma = sigma

        # Fit on total times
        total_ts = np.array([a.time_ms for a in completed], dtype=float)
        ta, tb, tc, tsigma = _fit_exp_decay(ns, total_ts)
        state.total_amplitude = ta
        state.total_decay_rate = tb
        state.total_asymptote = tc
        state.total_sigma = tsigma

        return state

    def init_state(self, first_attempt: AttemptRecord, priors: dict) -> ModelBState:
        return ModelBState(n_completed=1, n_attempts=1)

    def process_attempt(
        self, state: ModelBState, new_attempt: AttemptRecord,
        all_attempts: list[AttemptRecord],
    ) -> ModelBState:
        n_completed = state.n_completed + (1 if new_attempt.completed else 0)
        completed = [a for a in all_attempts if a.completed and a.time_ms is not None]
        new_state = self._run_fits(completed)
        new_state.n_completed = n_completed
        new_state.n_attempts = state.n_attempts + 1
        return new_state

    def model_output(self, state: ModelBState, all_attempts: list[AttemptRecord]) -> ModelOutput:
        completed = [a for a in all_attempts if a.completed and a.time_ms is not None]
        if not completed:
            return ModelOutput(0.0, 0.0, 0.0, 0.0, 0.0)

        total_times = [a.time_ms for a in completed]
        clean_tails = [a.clean_tail_ms for a in completed]
        n = len(completed)

        # Not enough data for a fit — fall back to simple stats
        if n < MIN_POINTS_FOR_FIT:
            med_total = float(statistics.median(total_times))
            med_clean = float(statistics.median(clean_tails))
            return ModelOutput(
                expected_time_ms=med_total,
                clean_expected_ms=med_clean,
                ms_per_attempt=0.0,
                floor_estimate_ms=float(min(total_times)),
                clean_floor_estimate_ms=float(min(clean_tails)),
            )

        current_n = float(n - 1)

        # Clean tail fit outputs
        clean_expected = float(state.amplitude * np.exp(-state.decay_rate * current_n) + state.asymptote)
        clean_ms_per_attempt = float(state.amplitude * state.decay_rate * np.exp(-state.decay_rate * current_n))

        # Total time: use recent median for expected (not the curve, which may not capture deaths well)
        n_recent = min(max(3, int(n * 0.2)), n)
        expected_total = float(statistics.median(total_times[-n_recent:]))

        return ModelOutput(
            expected_time_ms=expected_total,
            clean_expected_ms=clean_expected,
            ms_per_attempt=clean_ms_per_attempt,
            floor_estimate_ms=max(0.0, state.total_asymptote),
            clean_floor_estimate_ms=max(0.0, state.asymptote),
        )

    def rebuild_state(self, attempts: list[AttemptRecord]) -> ModelBState:
        completed = [a for a in attempts if a.completed and a.time_ms is not None]
        state = self._run_fits(completed)
        state.n_completed = len(completed)
        state.n_attempts = len(attempts)
        return state
