# python/spinlab/estimators/rolling_mean.py
"""Rolling mean estimator (model-free)."""
from __future__ import annotations

import statistics
from dataclasses import dataclass

from spinlab.estimators import Estimator, EstimatorState, register_estimator
from spinlab.models import AttemptRecord, Estimate, ModelOutput


@dataclass
class RollingMeanState(EstimatorState):
    """Minimal bookkeeping. Rolling mean recomputes from all_attempts each time."""

    n_completed: int = 0
    n_attempts: int = 0

    def to_dict(self) -> dict:
        return {"n_completed": self.n_completed, "n_attempts": self.n_attempts}

    @classmethod
    def from_dict(cls, d: dict) -> "RollingMeanState":
        return cls(n_completed=d.get("n_completed", 0), n_attempts=d.get("n_attempts", 0))


EstimatorState.register_state("rolling_mean", RollingMeanState)


@register_estimator
class RollingMeanEstimator(Estimator):
    name = "rolling_mean"
    display_name = "Rolling Mean"

    def init_state(self, first_attempt: AttemptRecord, priors: dict) -> RollingMeanState:
        return RollingMeanState(n_completed=1, n_attempts=1)

    def process_attempt(
        self, state: RollingMeanState, new_attempt: AttemptRecord,
        all_attempts: list[AttemptRecord],
    ) -> RollingMeanState:
        n_completed = state.n_completed + (1 if new_attempt.completed else 0)
        return RollingMeanState(n_completed=n_completed, n_attempts=state.n_attempts + 1)

    def model_output(self, state: RollingMeanState, all_attempts: list[AttemptRecord]) -> ModelOutput:
        completed = [a for a in all_attempts if a.completed and a.time_ms is not None]
        if not completed:
            return ModelOutput(
                total=Estimate(expected_ms=None, ms_per_attempt=None, floor_ms=None),
                clean=Estimate(expected_ms=None, ms_per_attempt=None, floor_ms=None),
            )

        total_times = [a.time_ms for a in completed]
        clean_tails = [a.clean_tail_ms for a in completed if a.clean_tail_ms is not None]

        avg_total = statistics.mean(total_times)

        n = len(completed)
        if n >= 2:
            half = max(n // 2, 1)
            first_half = statistics.mean(total_times[:half])
            second_half = statistics.mean(total_times[half:])
            total_trend = (first_half - second_half) / half
        else:
            total_trend = None

        clean_estimate: Estimate
        if clean_tails:
            avg_clean = statistics.mean(clean_tails)
            if len(clean_tails) >= 2:
                half_c = max(len(clean_tails) // 2, 1)
                first_c = statistics.mean(clean_tails[:half_c])
                second_c = statistics.mean(clean_tails[half_c:])
                clean_trend = (first_c - second_c) / half_c
            else:
                clean_trend = None
            clean_estimate = Estimate(
                expected_ms=avg_clean,
                ms_per_attempt=clean_trend,
                floor_ms=float(min(clean_tails)),
            )
        else:
            clean_estimate = Estimate(expected_ms=None, ms_per_attempt=None, floor_ms=None)

        return ModelOutput(
            total=Estimate(
                expected_ms=avg_total,
                ms_per_attempt=total_trend,
                floor_ms=float(min(total_times)),
            ),
            clean=clean_estimate,
        )

    def rebuild_state(self, attempts: list[AttemptRecord]) -> RollingMeanState:
        n_completed = sum(1 for a in attempts if a.completed)
        return RollingMeanState(n_completed=n_completed, n_attempts=len(attempts))
