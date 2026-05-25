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
