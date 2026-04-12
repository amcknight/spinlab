# python/spinlab/estimators/exp_decay.py
"""Exponential decay estimator.

Fits time(n) = amplitude * exp(-decay_rate * n) + asymptote
via scipy.optimize.curve_fit. Two fits: one on total times, one on clean tails.
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass

import numpy as np
from scipy.optimize import OptimizeWarning, curve_fit

from spinlab.estimators import Estimator, EstimatorState, register_estimator
from spinlab.models import AttemptRecord, Estimate, ModelOutput

MIN_POINTS_FOR_FIT = 3


def _exp_decay(n: np.ndarray, amplitude: float, decay_rate: float, asymptote: float) -> np.ndarray:
    return amplitude * np.exp(-decay_rate * n) + asymptote


def _fit_exp_decay(ns: np.ndarray, ts: np.ndarray) -> tuple[float, float, float, float]:
    """Fit amplitude*exp(-decay_rate*n)+asymptote. Returns (amplitude, decay_rate, asymptote, sigma).

    The asymptote is allowed to go below the observed minimum so the
    exponential can approximate near-linear improvement (where the true
    floor hasn't been reached yet).
    """
    best = float(np.min(ts))
    initial_amplitude = max(float(np.median(ts)) - best, 1.0)
    try:
        # We discard the covariance matrix, so scipy's OptimizeWarning
        # ("Covariance of the parameters could not be estimated") is noise —
        # it fires on small or near-degenerate inputs where the fit itself
        # is still valid for our purposes.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", OptimizeWarning)
            popt, _ = curve_fit(
                _exp_decay, ns, ts,
                p0=[initial_amplitude, 0.05, best],
                bounds=([0, 0, 0], [np.inf, np.inf, np.inf]),
            )
        amplitude, decay_rate, asymptote = popt
        residuals = ts - _exp_decay(ns, amplitude, decay_rate, asymptote)
        sigma = float(np.std(residuals))
        return float(amplitude), float(decay_rate), float(asymptote), sigma
    except RuntimeError:
        return initial_amplitude, 0.0, best, float(np.std(ts))


@dataclass
class ExpDecayState(EstimatorState):
    """Bookkeeping + cached fit params."""

    n_completed: int = 0
    n_attempts: int = 0
    amplitude: float = 0.0
    decay_rate: float = 0.0
    asymptote: float = 0.0
    sigma: float = 0.0
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
    def from_dict(cls, d: dict) -> "ExpDecayState":
        return cls(
            n_completed=d.get("n_completed", 0), n_attempts=d.get("n_attempts", 0),
            amplitude=d.get("amplitude", 0.0), decay_rate=d.get("decay_rate", 0.0),
            asymptote=d.get("asymptote", 0.0), sigma=d.get("sigma", 0.0),
            total_amplitude=d.get("total_amplitude", 0.0),
            total_decay_rate=d.get("total_decay_rate", 0.0),
            total_asymptote=d.get("total_asymptote", 0.0),
            total_sigma=d.get("total_sigma", 0.0),
        )


EstimatorState.register_state("exp_decay", ExpDecayState)


@register_estimator
class ExpDecayEstimator(Estimator):
    name = "exp_decay"
    display_name = "Exp. Decay"

    def _run_fits(self, completed: list[AttemptRecord]) -> ExpDecayState:
        state = ExpDecayState(n_completed=len(completed), n_attempts=len(completed))
        if len(completed) < MIN_POINTS_FOR_FIT:
            return state

        ns = np.arange(len(completed), dtype=float)

        clean_ts = np.array([a.clean_tail_ms if a.clean_tail_ms is not None else a.time_ms
                             for a in completed], dtype=float)
        a, b, c, sigma = _fit_exp_decay(ns, clean_ts)
        state.amplitude = a
        state.decay_rate = b
        state.asymptote = c
        state.sigma = sigma

        total_ts = np.array([att.time_ms for att in completed], dtype=float)
        ta, tb, tc, tsigma = _fit_exp_decay(ns, total_ts)
        state.total_amplitude = ta
        state.total_decay_rate = tb
        state.total_asymptote = tc
        state.total_sigma = tsigma

        return state

    def init_state(self, first_attempt: AttemptRecord, priors: dict, params: dict | None = None) -> ExpDecayState:
        return ExpDecayState(n_completed=1, n_attempts=1)

    def process_attempt(
        self, state: ExpDecayState, new_attempt: AttemptRecord,
        all_attempts: list[AttemptRecord],
        params: dict | None = None,
    ) -> ExpDecayState:
        n_completed = state.n_completed + (1 if new_attempt.completed else 0)
        completed = [a for a in all_attempts if a.completed and a.time_ms is not None]
        new_state = self._run_fits(completed)
        new_state.n_completed = n_completed
        new_state.n_attempts = state.n_attempts + 1
        return new_state

    def model_output(self, state: ExpDecayState, all_attempts: list[AttemptRecord]) -> ModelOutput:
        completed = [a for a in all_attempts if a.completed and a.time_ms is not None]
        n = len(completed)

        none_estimate = Estimate(expected_ms=None, ms_per_attempt=None, floor_ms=None)

        if n < MIN_POINTS_FOR_FIT:
            return ModelOutput(total=none_estimate, clean=none_estimate)

        next_n = float(n)  # predict at index n (next unobserved)

        # Total time fit
        total_expected = float(state.total_amplitude * np.exp(-state.total_decay_rate * next_n) + state.total_asymptote)
        total_next_next = float(state.total_amplitude * np.exp(-state.total_decay_rate * (next_n + 1)) + state.total_asymptote)
        total_mpa = total_expected - total_next_next  # discrete difference, positive = improving

        # Clean tail fit
        clean_expected = float(state.amplitude * np.exp(-state.decay_rate * next_n) + state.asymptote)
        clean_next_next = float(state.amplitude * np.exp(-state.decay_rate * (next_n + 1)) + state.asymptote)
        clean_mpa = clean_expected - clean_next_next

        return ModelOutput(
            total=Estimate(
                expected_ms=total_expected,
                ms_per_attempt=total_mpa,
                floor_ms=state.total_asymptote if state.total_asymptote > 0 else None,
            ),
            clean=Estimate(
                expected_ms=clean_expected,
                ms_per_attempt=clean_mpa,
                floor_ms=state.asymptote if state.asymptote > 0 else None,
            ),
        )

    def rebuild_state(self, attempts: list[AttemptRecord], params: dict | None = None) -> ExpDecayState:
        completed = [a for a in attempts if a.completed and a.time_ms is not None]
        state = self._run_fits(completed)
        state.n_completed = len(completed)
        state.n_attempts = len(attempts)
        return state
