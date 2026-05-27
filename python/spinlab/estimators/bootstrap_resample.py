"""Bootstrap-resample estimator.

Estimates per-segment attempt and completion times by resampling whole
cold episodes from recent history. Sidesteps the i.i.d.-lives assumption
baked into `death_aware_rolling`'s geometric formula; the divergence
between the two estimators is itself a diagnostic of how clustered the
deaths are.

See docs/superpowers/specs/2026-05-27-bootstrap-estimator-design.md.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import TYPE_CHECKING

from spinlab.estimators import Estimator, EstimatorState, ParamDef, register_estimator
from spinlab.estimators.death_aware_rolling import (
    DEFAULT_HALFLIFE,
    HALFLIFE_MAX,
    HALFLIFE_MIN,
)
from spinlab.models import (
    AttemptRecord,
    Estimate,
    ModelOutput,
)

if TYPE_CHECKING:
    from spinlab.estimators._episode_helpers import _Episode
    from spinlab.models import EventAttempt

# Bootstrap draw count. 1000 is enough to bring the Monte-Carlo standard
# error well below 1% of the mean for realistic distributions while
# staying well under any noticeable per-tick CPU cost.
DEFAULT_N_SAMPLES = 1000

# Sanity bounds. Lower bound 100 keeps Monte-Carlo error from dominating
# the signal; upper bound 10000 caps per-call cost. Anything outside this
# range almost certainly means the user is using the wrong tool.
N_SAMPLES_MIN = 100
N_SAMPLES_MAX = 10000


def _filter_to_cold_episodes(episodes: list["_Episode"]) -> list["_Episode"]:
    """Keep only episodes where every life is cold (is_hot is False).

    Hot lives represent a different population (carry-over from a prior
    completed segment, not a fresh-load attempt). The scheduler's
    "next practice load" decision is a cold-load question, so cold-only
    is the right resampling pool. Mixed-state episodes are dropped
    entirely rather than half-counted — partial inclusion would
    contaminate the sample with hot-life timing within a "cold" draw.
    """
    return [
        ep for ep in episodes
        if all(not ev.is_hot for ev in ep.events)
    ]


@dataclass
class BootstrapResampleState(EstimatorState):
    """Minimal bookkeeping. Stats recompute from events each call."""

    def to_dict(self) -> dict:
        return {"n_completed": self.n_completed, "n_attempts": self.n_attempts}

    @classmethod
    def from_dict(cls, d: dict) -> "BootstrapResampleState":
        return cls(
            n_completed=d.get("n_completed", 0),
            n_attempts=d.get("n_attempts", 0),
        )


EstimatorState.register_state("bootstrap_resample", BootstrapResampleState)


def _empty_output() -> ModelOutput:
    none_estimate = Estimate(expected_ms=None, ms_per_attempt=None, floor_ms=None)
    return ModelOutput(total=none_estimate, clean=none_estimate, extras=None)


@register_estimator
class BootstrapResampleEstimator(Estimator):
    name = "bootstrap_resample"
    display_name = "Bootstrap (Monte Carlo)"

    def __init__(self, seed: int | None = None) -> None:
        # Seedable RNG for deterministic tests. Default None = nondeterministic.
        self._rng = random.Random(seed)

    def declared_params(self) -> list[ParamDef]:
        return [
            ParamDef(
                "halflife", "Halflife (episodes)",
                float(DEFAULT_HALFLIFE), float(HALFLIFE_MIN), float(HALFLIFE_MAX), 1.0,
                "Number of episodes for the sampling weight to halve. "
                "Mirrors death_aware_rolling so the two estimators see the "
                "same effective window.",
            ),
            ParamDef(
                "n_samples", "Bootstrap draws",
                float(DEFAULT_N_SAMPLES), float(N_SAMPLES_MIN), float(N_SAMPLES_MAX), 100.0,
                "Number of resampled episodes drawn per estimate. Higher = "
                "smaller Monte-Carlo error, linear cost.",
            ),
        ]

    def init_state(
        self, first_attempt: AttemptRecord, priors: dict,
        params: dict | None = None,
    ) -> BootstrapResampleState:
        return BootstrapResampleState(n_completed=1, n_attempts=1)

    def process_attempt(  # type: ignore[override]
        self, state: BootstrapResampleState, new_attempt: AttemptRecord,
        all_attempts: list[AttemptRecord],
        params: dict | None = None,
        events: list["EventAttempt"] | None = None,
    ) -> BootstrapResampleState:
        n_completed = state.n_completed + (1 if new_attempt.completed else 0)
        return BootstrapResampleState(
            n_completed=n_completed, n_attempts=state.n_attempts + 1,
        )

    def model_output(  # type: ignore[override]
        self, state: BootstrapResampleState, all_attempts: list[AttemptRecord],
        params: dict | None = None,
        events: list["EventAttempt"] | None = None,
    ) -> ModelOutput:
        # Placeholder — wired up in Task 4+. Returning empty keeps the
        # estimator registered without producing misleading numbers.
        return _empty_output()

    def rebuild_state(  # type: ignore[override]
        self, attempts: list[AttemptRecord],
        params: dict | None = None,
        events: list["EventAttempt"] | None = None,
    ) -> BootstrapResampleState:
        n_completed = sum(1 for a in attempts if a.completed)
        return BootstrapResampleState(
            n_completed=n_completed, n_attempts=len(attempts),
        )
