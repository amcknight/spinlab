"""Death-aware rolling estimator.

Tracks decayed rolling statistics per segment: death rate (per-life and
per-attempt), death-time distribution, and completion-time distribution.
Populates ModelOutput.total and ModelOutput.clean via the geometric
expected-time formula, plus a DeathExtras payload carrying the
distribution samples and the two p_die quantities.

State is recomputed from events on every call (rolling_mean style); only
the n_completed / n_attempts counters live in state_json. The halflife
knob lives in declared_params.

See docs/superpowers/specs/2026-05-24-death-aware-rolling-design.md for
the math and the geometric/recursive derivation.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from spinlab.estimators import Estimator, EstimatorState, ParamDef, register_estimator
from spinlab.estimators._episode_helpers import (
    _Episode,
    _compute_weights,
    _group_into_episodes,
)
from spinlab.models import (
    DEFAULT_DEATH_PENALTY_MS,
    AttemptRecord,
    DeathExtras,
    Estimate,
    ModelOutput,
)

if TYPE_CHECKING:
    from spinlab.models import EventAttempt

# Default halflife in episodes. ~20 ≈ a recent month of casual practice at
# typical session cadence; low enough to track a player improving week-over-
# week, high enough to avoid thrashing on a single bad session.
DEFAULT_HALFLIFE = 20

# Effective window: episodes beyond this many halflives are dropped before
# computing stats.  At 5×halflife, episode weight is 2^{-5} ≈ 0.031 (~3%).
# Episodes contributing this little don't move outputs materially, so the
# cutoff just avoids iterating over arbitrarily old history.
EFFECTIVE_WINDOW_HALFLIVES = 5

# Halflife bounds. Lower bound = 1 episode (halving every episode is the most
# responsive sensible setting; lower violates the exponential-decay math since
# the formula 2**(-(N-i)/halflife) is undefined for halflife=0). Upper bound =
# 200 episodes (at typical practice cadence this corresponds to multiple weeks
# of effective memory; above this the rolling estimator stops being "rolling"
# in any meaningful sense and approaches plain mean.)
HALFLIFE_MIN = 1
HALFLIFE_MAX = 200


@dataclass
class DeathAwareRollingState(EstimatorState):
    """Minimal bookkeeping. Stats recompute from events each call."""

    def to_dict(self) -> dict:
        return {"n_completed": self.n_completed, "n_attempts": self.n_attempts}

    @classmethod
    def from_dict(cls, d: dict) -> "DeathAwareRollingState":
        return cls(
            n_completed=d.get("n_completed", 0),
            n_attempts=d.get("n_attempts", 0),
        )


EstimatorState.register_state("death_aware_rolling", DeathAwareRollingState)


@dataclass
class _Aggregates:
    """Holds all the rolling statistics for one segment.

    Internal — produced by _compute_aggregates and consumed by model_output
    (Task 7) to compose ModelOutput + DeathExtras.

    Episode-level counts (n_episodes_with_death_eff / n_episodes_completed_eff)
    are NOT complementary: an episode can both contain deaths AND complete,
    so their sum can exceed n_attempts_effective.
    """
    halflife: int
    # Episode-level
    n_attempts_effective: float
    n_episodes_with_death_eff: float
    n_episodes_completed_eff: float
    p_die_per_attempt: float | None
    # Life-level
    n_lives_died_effective: float
    n_lives_survived_effective: float
    p_die_per_life: float | None
    # Distributions (life-level samples)
    death_samples: list[tuple[int, float]]
    completion_samples: list[tuple[int, float]]
    expected_death_time_ms: float | None
    expected_completion_time_ms: float | None


def _weighted_mean(samples: list[tuple[int, float]]) -> float | None:
    """Weighted mean of (value, weight) pairs. Returns None when empty or all-zero-weight."""
    if not samples:
        return None
    total_w = sum(w for _, w in samples)
    if total_w == 0:
        return None
    return sum(t * w for t, w in samples) / total_w


def _compute_aggregates(
    events: list["EventAttempt"], halflife: int,
) -> _Aggregates:
    """Compute all rolling statistics for a segment from its event list.

    Drops invalidated episodes (via _group_into_episodes), truncates the
    working set to ~5×halflife episodes (older episodes contribute weight
    < ~3% and don't move outputs materially), then computes both life-level
    and episode-level aggregates from the same weighted dataset.

    Truncation is INLINE here — there is no separate truncate helper.
    """
    episodes = _group_into_episodes(events)
    if not episodes:
        return _Aggregates(
            halflife=halflife,
            n_attempts_effective=0.0,
            n_episodes_with_death_eff=0.0,
            n_episodes_completed_eff=0.0,
            p_die_per_attempt=None,
            n_lives_died_effective=0.0,
            n_lives_survived_effective=0.0,
            p_die_per_life=None,
            death_samples=[],
            completion_samples=[],
            expected_death_time_ms=None,
            expected_completion_time_ms=None,
        )

    # Truncate to the effective window. Older episodes' weights are < ~3%
    # at the cap and don't move outputs within float precision; dropping
    # them keeps the working set bounded.
    max_kept = EFFECTIVE_WINDOW_HALFLIVES * halflife
    if len(episodes) > max_kept:
        episodes = episodes[-max_kept:]

    weights = _compute_weights(n_episodes=len(episodes), halflife=halflife)

    n_attempts_effective = sum(weights)
    n_episodes_with_death_eff = sum(
        w for w, ep in zip(weights, episodes) if ep.had_any_death
    )
    n_episodes_completed_eff = sum(
        w for w, ep in zip(weights, episodes) if ep.outcome == "completed"
    )
    p_die_per_attempt = (
        n_episodes_with_death_eff / n_attempts_effective
        if n_attempts_effective > 0 else None
    )

    death_samples: list[tuple[int, float]] = []
    completion_samples: list[tuple[int, float]] = []
    for w, ep in zip(weights, episodes):
        for ev in ep.events:
            sample = (int(ev.time_ms), w)
            if ev.outcome.value == "died":
                death_samples.append(sample)
            else:
                completion_samples.append(sample)

    n_lives_died_effective = sum(w for _, w in death_samples)
    n_lives_survived_effective = sum(w for _, w in completion_samples)
    total_life_weight = n_lives_died_effective + n_lives_survived_effective
    p_die_per_life = (
        n_lives_died_effective / total_life_weight
        if total_life_weight > 0 else None
    )

    return _Aggregates(
        halflife=halflife,
        n_attempts_effective=n_attempts_effective,
        n_episodes_with_death_eff=n_episodes_with_death_eff,
        n_episodes_completed_eff=n_episodes_completed_eff,
        p_die_per_attempt=p_die_per_attempt,
        n_lives_died_effective=n_lives_died_effective,
        n_lives_survived_effective=n_lives_survived_effective,
        p_die_per_life=p_die_per_life,
        death_samples=death_samples,
        completion_samples=completion_samples,
        expected_death_time_ms=_weighted_mean(death_samples),
        expected_completion_time_ms=_weighted_mean(completion_samples),
    )


def _expected_total_ms(
    p_die_per_life: float | None,
    e_death_time_ms: float | None,
    e_completion_time_ms: float | None,
    respawn_penalty_ms: int,
) -> float | None:
    """Geometric formula for expected attempt time.

    E[attempt] = (p / (1-p)) * (E[death] + penalty) + E[completion]

    where p = p_die_per_life. Each life is modeled as independent Bernoulli;
    player retries until completion. The expected number of death lives
    before completion is geometric with mean p / (1 - p).

    Returns None when the projection isn't well-defined:
      - p_die_per_life is None (no events observed)
      - p_die_per_life is 1.0 (no completions ⇒ can't project completion time)
      - e_completion_time_ms is None (haven't seen a completion yet)
      - p_die_per_life > 0 but e_death_time_ms is None (inconsistent input;
        shouldn't happen in practice but guard anyway)
    """
    if p_die_per_life is None or e_completion_time_ms is None:
        return None
    if p_die_per_life >= 1.0:
        return None
    if p_die_per_life > 0 and e_death_time_ms is None:
        return None
    if p_die_per_life == 0:
        return e_completion_time_ms
    # p_die_per_life ∈ (0, 1) here, and e_death_time_ms is not None.
    assert e_death_time_ms is not None  # guarded above; assert for type narrowing
    q = 1.0 - p_die_per_life
    e_n_death_lives = p_die_per_life / q
    return e_n_death_lives * (e_death_time_ms + respawn_penalty_ms) + e_completion_time_ms


def _weighted_half_split_slope(
    samples: list[tuple[int, float]],
) -> float | None:
    """Crude slope estimator: (mean_first_half - mean_second_half) / half_n.

    Operates on a chronologically-ordered sample list. Positive ⇒ improving
    (earlier samples were slower than later samples). Returns None when there
    are fewer than 2 samples (no slope is defined).

    Both halves use weighted means so the underlying decay weighting is
    threaded through the trend calculation.
    """
    if len(samples) < 2:
        return None
    half = max(len(samples) // 2, 1)
    first = samples[:half]
    second = samples[half:]
    m_first = _weighted_mean(first)
    m_second = _weighted_mean(second)
    if m_first is None or m_second is None:
        return None
    return (m_first - m_second) / half


def _floor_over_completed_episode_totals(
    episodes: list[_Episode], respawn_penalty_ms: int,
) -> float | None:
    """Min episode_total_time_ms across all completed episodes (not windowed).

    episode_total_time_ms = sum(event.time_ms) + respawn_penalty_ms × n_deaths.
    Matches the production roll-up in spinlab.db.attempts._roll_up_episode.

    Not windowed — best-ever total is sticky info, even if the great episode
    happened long ago.
    """
    best: float | None = None
    for ep in episodes:
        if ep.outcome != "completed":
            continue
        deaths = sum(1 for ev in ep.events if ev.outcome.value == "died")
        total = sum(ev.time_ms for ev in ep.events) + respawn_penalty_ms * deaths
        if best is None or total < best:
            best = float(total)
    return best


def _floor_over_survived_event_times(episodes: list[_Episode]) -> float | None:
    """Min survived-event time_ms across all survived events (not windowed)."""
    best: float | None = None
    for ep in episodes:
        for ev in ep.events:
            if ev.outcome.value != "survived":
                continue
            if best is None or ev.time_ms < best:
                best = float(ev.time_ms)
    return best


def _resolve_halflife(params: dict | None) -> int:
    if not params or "halflife" not in params:
        return DEFAULT_HALFLIFE
    raw = params["halflife"]
    try:
        n = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"halflife must be an int, got {raw!r}") from exc
    if n < HALFLIFE_MIN or n > HALFLIFE_MAX:
        raise ValueError(f"halflife must be in [{HALFLIFE_MIN}, {HALFLIFE_MAX}], got {n}")
    return n


def _empty_output() -> ModelOutput:
    """Output shape for empty / fully-invalidated event lists."""
    none_estimate = Estimate(expected_ms=None, ms_per_attempt=None, floor_ms=None)
    return ModelOutput(total=none_estimate, clean=none_estimate, extras=None)


@register_estimator
class DeathAwareRollingEstimator(Estimator):
    name = "death_aware_rolling"
    display_name = "Death-Aware Rolling"

    def declared_params(self) -> list[ParamDef]:
        return [
            ParamDef(
                "halflife", "Halflife (episodes)",
                float(DEFAULT_HALFLIFE), float(HALFLIFE_MIN), float(HALFLIFE_MAX), 1.0,
                "Number of episodes for the rolling weight to halve. "
                "20 ≈ recent month of casual practice; lower = more "
                "responsive to recent changes, higher = more stable.",
            ),
        ]

    def init_state(
        self, first_attempt: AttemptRecord, priors: dict,
        params: dict | None = None,
    ) -> DeathAwareRollingState:
        return DeathAwareRollingState(n_completed=1, n_attempts=1)

    def process_attempt(  # type: ignore[override]
        self, state: DeathAwareRollingState, new_attempt: AttemptRecord,
        all_attempts: list[AttemptRecord],
        params: dict | None = None,
        events: list["EventAttempt"] | None = None,
    ) -> DeathAwareRollingState:
        n_completed = state.n_completed + (1 if new_attempt.completed else 0)
        return DeathAwareRollingState(
            n_completed=n_completed, n_attempts=state.n_attempts + 1,
        )

    def model_output(  # type: ignore[override]
        self, state: DeathAwareRollingState, all_attempts: list[AttemptRecord],
        params: dict | None = None,
        events: list["EventAttempt"] | None = None,
    ) -> ModelOutput:
        if not events:
            return _empty_output()
        halflife = _resolve_halflife(params)

        # All episodes (used for floor_ms across full history — not windowed).
        all_episodes = _group_into_episodes(events)
        if not all_episodes:
            return _empty_output()

        agg = _compute_aggregates(events, halflife=halflife)

        total_expected_ms = _expected_total_ms(
            p_die_per_life=agg.p_die_per_life,
            e_death_time_ms=agg.expected_death_time_ms,
            e_completion_time_ms=agg.expected_completion_time_ms,
            respawn_penalty_ms=DEFAULT_DEATH_PENALTY_MS,
        )
        clean_expected_ms = agg.expected_completion_time_ms

        # ms_per_attempt: slope over completion_samples in chronological order.
        # The samples list is already chronological because episodes are
        # ordered chronologically in _compute_aggregates and each episode's
        # events are inserted in order.
        clean_mpa = _weighted_half_split_slope(agg.completion_samples)
        # For total, slope is over the same completion samples — total tracks
        # completion-time learning + death-rate trends together; a richer
        # slope estimator is a follow-up.
        total_mpa = clean_mpa

        total_floor = _floor_over_completed_episode_totals(
            all_episodes, respawn_penalty_ms=DEFAULT_DEATH_PENALTY_MS,
        )
        clean_floor = _floor_over_survived_event_times(all_episodes)

        extras = DeathExtras(
            halflife_attempts=halflife,
            n_attempts_effective=agg.n_attempts_effective,
            n_episodes_with_death_eff=agg.n_episodes_with_death_eff,
            n_episodes_completed_eff=agg.n_episodes_completed_eff,
            p_die_per_attempt=agg.p_die_per_attempt,
            n_lives_died_effective=agg.n_lives_died_effective,
            n_lives_survived_effective=agg.n_lives_survived_effective,
            p_die_per_life=agg.p_die_per_life,
            death_samples=agg.death_samples,
            completion_samples=agg.completion_samples,
            expected_death_time_ms=agg.expected_death_time_ms,
            expected_completion_time_ms=agg.expected_completion_time_ms,
        )
        return ModelOutput(
            total=Estimate(
                expected_ms=total_expected_ms,
                ms_per_attempt=total_mpa,
                floor_ms=total_floor,
            ),
            clean=Estimate(
                expected_ms=clean_expected_ms,
                ms_per_attempt=clean_mpa,
                floor_ms=clean_floor,
            ),
            extras=extras,
        )

    def rebuild_state(  # type: ignore[override]
        self, attempts: list[AttemptRecord],
        params: dict | None = None,
        events: list["EventAttempt"] | None = None,
    ) -> DeathAwareRollingState:
        n_completed = sum(1 for a in attempts if a.completed)
        return DeathAwareRollingState(
            n_completed=n_completed, n_attempts=len(attempts),
        )
