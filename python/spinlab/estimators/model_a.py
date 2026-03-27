# python/spinlab/estimators/model_a.py
"""Model A: Rolling statistics estimator (model-free)."""
from __future__ import annotations

import statistics
from dataclasses import dataclass

from spinlab.estimators import Estimator, EstimatorState, register_estimator
from spinlab.models import AttemptRecord, ModelOutput


@dataclass
class ModelAState(EstimatorState):
    """Minimal bookkeeping. Model A recomputes from all_attempts each time."""

    n_completed: int = 0
    n_attempts: int = 0

    def to_dict(self) -> dict:
        return {"n_completed": self.n_completed, "n_attempts": self.n_attempts}

    @classmethod
    def from_dict(cls, d: dict) -> "ModelAState":
        return cls(n_completed=d.get("n_completed", 0), n_attempts=d.get("n_attempts", 0))


@register_estimator
class ModelAEstimator(Estimator):
    name = "model_a"

    def init_state(self, first_attempt: AttemptRecord, priors: dict) -> ModelAState:
        return ModelAState(n_completed=1, n_attempts=1)

    def process_attempt(
        self, state: ModelAState, new_attempt: AttemptRecord,
        all_attempts: list[AttemptRecord],
    ) -> ModelAState:
        n_completed = state.n_completed + (1 if new_attempt.completed else 0)
        return ModelAState(n_completed=n_completed, n_attempts=state.n_attempts + 1)

    def model_output(self, state: ModelAState, all_attempts: list[AttemptRecord]) -> ModelOutput:
        completed = [a for a in all_attempts if a.completed and a.time_ms is not None]
        if not completed:
            return ModelOutput(0.0, 0.0, 0.0, 0.0, 0.0)

        total_times = [a.time_ms for a in completed]
        clean_tails = [a.clean_tail_ms for a in completed]
        n = len(completed)

        # Window sizes
        n_recent = min(max(3, int(n * 0.2)), n)
        n_broad = min(max(5, int(n * 0.5)), n)

        recent_total = total_times[-n_recent:]
        recent_clean = clean_tails[-n_recent:]
        recent_median_total = statistics.median(recent_total)
        recent_median_clean = statistics.median(recent_clean)

        # Trend from clean tails
        if n_broad > n_recent:
            broad_clean = clean_tails[-n_broad:]
            broad_median_clean = statistics.median(broad_clean)
            attempt_gap = (n_broad - n_recent) / 2.0
            trend = (recent_median_clean - broad_median_clean) / attempt_gap
            ms_per_attempt = -trend
        else:
            ms_per_attempt = 0.0

        return ModelOutput(
            expected_time_ms=recent_median_total,
            clean_expected_ms=recent_median_clean,
            ms_per_attempt=ms_per_attempt,
            floor_estimate_ms=float(min(total_times)),
            clean_floor_estimate_ms=float(min(clean_tails)),
        )

    def rebuild_state(self, attempts: list[AttemptRecord]) -> ModelAState:
        n_completed = sum(1 for a in attempts if a.completed)
        return ModelAState(n_completed=n_completed, n_attempts=len(attempts))
