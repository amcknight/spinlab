"""Exponential decay estimator.

Fits time(n) = amplitude * exp(-decay_rate * n) + asymptote
via scipy.optimize.curve_fit. Two fits: one on total times, one on clean tails.
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np
from scipy.optimize import OptimizeWarning, curve_fit

from spinlab.estimators import (
    Estimator,
    EstimatorState,
    load_mature_states,
    register_estimator,
)
from spinlab.models import AttemptRecord, Estimate, ModelOutput

if TYPE_CHECKING:
    from spinlab.db import Database
    from spinlab.models import EventAttempt

MIN_POINTS_FOR_FIT = 3
# Mirror the Kalman estimator's bar — only segments with this many completions
# are considered "mature" enough to contribute to population priors.  Below the
# threshold the per-segment fit is too noisy to help anyone else.
MATURITY_THRESHOLD = 10
# Default decay-rate seed when no priors are available.  0.05 = ~5% improvement
# per attempt early on, decaying smoothly.  Empirically reasonable for SMW
# splits with ~10 attempts of practice; anything smaller barely curves.
DEFAULT_DECAY_RATE_SEED = 0.05


def _exp_decay(n: np.ndarray, amplitude: float, decay_rate: float, asymptote: float) -> np.ndarray:
    return amplitude * np.exp(-decay_rate * n) + asymptote


def _fit_exp_decay(
    ns: np.ndarray, ts: np.ndarray,
    p0_seed: tuple[float, float, float] | None = None,
) -> tuple[float, float, float, float]:
    """Fit amplitude*exp(-decay_rate*n)+asymptote.

    Returns (amplitude, decay_rate, asymptote, sigma).  If ``p0_seed`` is given,
    those values prime the optimizer instead of the default heuristics — useful
    when population priors give a better starting point than per-segment guesses.

    The asymptote is allowed to go below the observed minimum so the
    exponential can approximate near-linear improvement (where the true
    floor hasn't been reached yet).
    """
    best = float(np.min(ts))
    initial_amplitude = max(float(np.median(ts)) - best, 1.0)
    p0 = list(p0_seed) if p0_seed else [initial_amplitude, DEFAULT_DECAY_RATE_SEED, best]
    try:
        # We discard the covariance matrix, so scipy's OptimizeWarning
        # ("Covariance of the parameters could not be estimated") is noise —
        # it fires on small or near-degenerate inputs where the fit itself
        # is still valid for our purposes.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", OptimizeWarning)
            popt, _ = curve_fit(
                _exp_decay, ns, ts,
                p0=p0,
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

    def _run_fits(
        self, completed: list[AttemptRecord], priors: dict | None = None,
    ) -> ExpDecayState:
        state = ExpDecayState(n_completed=len(completed), n_attempts=len(completed))
        if len(completed) < MIN_POINTS_FOR_FIT:
            # Honest: not enough data to fit.  Leave fit params at zero so
            # model_output returns None.  Priors don't get to fake an estimate.
            return state

        ns = np.arange(len(completed), dtype=float)
        # Population priors prime the optimizer's initial guess, but the fit
        # is still driven by this segment's data — bounds + curve_fit will
        # move away from the seed when observations contradict it.
        clean_seed = (
            (priors["amplitude"], priors["decay_rate"], priors["asymptote"])
            if priors and {"amplitude", "decay_rate", "asymptote"} <= priors.keys()
            else None
        )
        total_seed = (
            (priors["total_amplitude"], priors["total_decay_rate"], priors["total_asymptote"])
            if priors and {"total_amplitude", "total_decay_rate", "total_asymptote"} <= priors.keys()
            else None
        )

        clean_ts = np.array([a.clean_tail_ms if a.clean_tail_ms is not None else a.time_ms
                             for a in completed], dtype=float)
        a, b, c, sigma = _fit_exp_decay(ns, clean_ts, p0_seed=clean_seed)
        state.amplitude = a
        state.decay_rate = b
        state.asymptote = c
        state.sigma = sigma

        total_ts = np.array([att.time_ms for att in completed], dtype=float)
        ta, tb, tc, tsigma = _fit_exp_decay(ns, total_ts, p0_seed=total_seed)
        state.total_amplitude = ta
        state.total_decay_rate = tb
        state.total_asymptote = tc
        state.total_sigma = tsigma

        return state

    def init_state(self, first_attempt: AttemptRecord, priors: dict, params: dict | None = None) -> ExpDecayState:
        # Priors are not used to fabricate fit params here — with one
        # observation the curve isn't determined.  They flow through to
        # process_attempt -> _run_fits as the curve_fit p0 seed once we
        # cross MIN_POINTS_FOR_FIT.
        return ExpDecayState(n_completed=1, n_attempts=1)

    def process_attempt(  # type: ignore[override]
        self, state: ExpDecayState, new_attempt: AttemptRecord,
        all_attempts: list[AttemptRecord],
        params: dict | None = None,
        events: list["EventAttempt"] | None = None,
    ) -> ExpDecayState:
        n_completed = state.n_completed + (1 if new_attempt.completed else 0)
        completed = [a for a in all_attempts if a.completed and a.time_ms is not None]
        # Once the fit has converged at least once (n_completed >= MIN_POINTS_FOR_FIT
        # at the time of the last fit), use the previous fit params as p0 so
        # successive fits converge smoothly.  Below that threshold we have no
        # local prior to carry, so leave p0 at the heuristic default.
        carry_priors = {
            "amplitude": state.amplitude, "decay_rate": state.decay_rate, "asymptote": state.asymptote,
            "total_amplitude": state.total_amplitude, "total_decay_rate": state.total_decay_rate,
            "total_asymptote": state.total_asymptote,
        } if state.n_completed >= MIN_POINTS_FOR_FIT else None
        new_state = self._run_fits(completed, priors=carry_priors)
        new_state.n_completed = n_completed
        new_state.n_attempts = state.n_attempts + 1
        return new_state

    def get_priors(self, db: "Database", game_id: str) -> dict:
        """Average fit params across all mature exp_decay states for this game."""
        mature = load_mature_states(db, game_id, "exp_decay", ExpDecayState, MATURITY_THRESHOLD)
        if not mature:
            return {}
        keys = ("amplitude", "decay_rate", "asymptote",
                "total_amplitude", "total_decay_rate", "total_asymptote")
        sums = dict.fromkeys(keys, 0.0)
        for s in mature:
            for k in keys:
                sums[k] += getattr(s, k)
        n = len(mature)
        return {k: v / n for k, v in sums.items()}

    def model_output(  # type: ignore[override]
        self, state: ExpDecayState, all_attempts: list[AttemptRecord],
        params: dict | None = None,
        events: list["EventAttempt"] | None = None,
    ) -> ModelOutput:
        completed = [a for a in all_attempts if a.completed and a.time_ms is not None]
        n = len(completed)

        none_estimate = Estimate(expected_ms=None, ms_per_attempt=None, floor_ms=None)

        if n < MIN_POINTS_FOR_FIT:
            return ModelOutput(total=none_estimate, clean=none_estimate)

        next_n = float(n)  # predict at index n (next unobserved)

        total_expected = float(state.total_amplitude * np.exp(-state.total_decay_rate * next_n) + state.total_asymptote)
        total_next_next = float(state.total_amplitude * np.exp(-state.total_decay_rate * (next_n + 1)) + state.total_asymptote)
        total_mpa = total_expected - total_next_next  # discrete difference, positive = improving

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

    def rebuild_state(self, attempts: list[AttemptRecord], params: dict | None = None,
                      events: list["EventAttempt"] | None = None) -> ExpDecayState:
        completed = [a for a in attempts if a.completed and a.time_ms is not None]
        state = self._run_fits(completed)
        state.n_completed = len(completed)
        state.n_attempts = len(attempts)
        return state
