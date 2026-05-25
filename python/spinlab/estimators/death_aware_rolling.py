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
from spinlab.models import AttemptRecord, DeathExtras, Estimate, ModelOutput

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
class _Episode:
    """Per-episode aggregated view used by the math layer.

    Internal — not part of the public API. Produced by _group_into_episodes
    and consumed by the aggregate helpers (Task 5+).
    """
    episode_id: str
    events: list["EventAttempt"]
    outcome: str       # "completed" if any event is survived, else "died"
    had_any_death: bool


def _group_into_episodes(events: list["EventAttempt"]) -> list[_Episode]:
    """Group events by episode_id, dropping any episode with an invalidated event.

    Episodes are returned in the chronological order their FIRST event arrived
    in the input list. The scheduler queries events via
    Database.get_segment_event_rows which returns rows ordered by row id
    (chronological insertion order), so the first occurrence of each
    episode_id reflects the episode's start time.

    Python dicts preserve insertion order (PEP 468 / 3.7+), so iterating
    by_id below yields episodes in their first-encounter order.
    """
    by_id: dict[str, list["EventAttempt"]] = {}
    for ev in events:
        by_id.setdefault(ev.episode_id, []).append(ev)

    episodes: list[_Episode] = []
    for ep_id, ev_list in by_id.items():
        if any(ev.invalidated for ev in ev_list):
            continue
        had_any_death = any(ev.outcome.value == "died" for ev in ev_list)
        any_survived = any(ev.outcome.value == "survived" for ev in ev_list)
        outcome = "completed" if any_survived else "died"
        episodes.append(_Episode(
            episode_id=ep_id, events=ev_list,
            outcome=outcome, had_any_death=had_any_death,
        ))
    return episodes


def _compute_weights(n_episodes: int, halflife: int) -> list[float]:
    """Return exponentially-decayed weights, one per episode.

    weights[i] = 2 ** (-(n_episodes - 1 - i) / halflife)

    The most-recent episode (index n_episodes - 1) has weight 1.0. An episode
    halflife steps back has weight 0.5.
    """
    return [
        2.0 ** (-(n_episodes - 1 - i) / halflife)
        for i in range(n_episodes)
    ]


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
    q = 1.0 - p_die_per_life
    e_n_death_lives = p_die_per_life / q
    return e_n_death_lives * (e_death_time_ms + respawn_penalty_ms) + e_completion_time_ms


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
        # Full math wired in Task 7.
        return _empty_output()

    def rebuild_state(  # type: ignore[override]
        self, attempts: list[AttemptRecord],
        params: dict | None = None,
        events: list["EventAttempt"] | None = None,
    ) -> DeathAwareRollingState:
        n_completed = sum(1 for a in attempts if a.completed)
        return DeathAwareRollingState(
            n_completed=n_completed, n_attempts=len(attempts),
        )
