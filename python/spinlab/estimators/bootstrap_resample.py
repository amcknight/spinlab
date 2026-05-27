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


def _episode_total_ms(episode: "_Episode", respawn_penalty_ms: int) -> int:
    """Compute the episode's wall-clock total, matching db.attempts._roll_up_episode.

    total = sum(event.time_ms) + respawn_penalty_ms × n_deaths

    Per-event time_ms is the raw delta since the prior event (or arm time
    for the first), so summing them gives the raw wall-clock; the penalty
    adds the standard 3.2s respawn lag for each death. The penalty value
    must match the one used at write time (DEFAULT_DEATH_PENALTY_MS).
    """
    deaths = sum(1 for ev in episode.events if ev.outcome.value == "died")
    raw_sum = sum(ev.time_ms for ev in episode.events)
    return raw_sum + respawn_penalty_ms * deaths


def _survived_tail_ms(episode: "_Episode") -> int | None:
    """Return the completion-tail time (last life's time_ms) or None.

    Only completed episodes have a tail — aborted episodes (all deaths)
    return None and the caller skips them when building the completion
    sample pool.
    """
    if episode.outcome != "completed":
        return None
    last = episode.events[-1]
    assert last.outcome.value == "survived"  # implied by outcome == "completed"
    return last.time_ms


def _resolve_n_samples(params: dict | None) -> int:
    if not params or "n_samples" not in params:
        return DEFAULT_N_SAMPLES
    raw = params["n_samples"]
    try:
        n = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"n_samples must be an int, got {raw!r}") from exc
    if n < N_SAMPLES_MIN or n > N_SAMPLES_MAX:
        raise ValueError(
            f"n_samples must be in [{N_SAMPLES_MIN}, {N_SAMPLES_MAX}], got {n}"
        )
    return n


@dataclass
class _BootstrapResult:
    """Means computed across the bootstrap draws.

    Both fields are None when the corresponding pool is empty:
      - mean_total_ms: None ⇔ no episodes at all
      - mean_completion_ms: None ⇔ no COMPLETED episodes (all aborted)
    """
    mean_total_ms: float | None
    mean_completion_ms: float | None


def _bootstrap_means(
    episodes: list["_Episode"],
    weights: list[float],
    n_samples: int,
    respawn_penalty_ms: int,
    rng: random.Random,
) -> _BootstrapResult:
    """Draw n_samples episodes from `episodes` with the given weights,
    then return mean per-draw total time and mean completion-tail time.

    The completion mean is computed over only the completed-episode draws
    (aborted draws contribute None and are filtered out). When zero draws
    land on a completed episode, mean_completion_ms is None.
    """
    if not episodes:
        return _BootstrapResult(mean_total_ms=None, mean_completion_ms=None)

    # Precompute per-episode totals and tails once — saves O(n_samples × episode_len)
    # work versus computing inside the resample loop.
    totals = [_episode_total_ms(ep, respawn_penalty_ms) for ep in episodes]
    tails = [_survived_tail_ms(ep) for ep in episodes]

    draws = rng.choices(range(len(episodes)), weights=weights, k=n_samples)

    total_sum = 0.0
    completion_sum = 0.0
    completion_count = 0
    for idx in draws:
        total_sum += totals[idx]
        tail = tails[idx]
        if tail is not None:
            completion_sum += tail
            completion_count += 1

    mean_total = total_sum / n_samples
    mean_completion = (
        completion_sum / completion_count if completion_count > 0 else None
    )
    return _BootstrapResult(
        mean_total_ms=mean_total, mean_completion_ms=mean_completion,
    )


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
