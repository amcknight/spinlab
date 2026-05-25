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

# Effective window: episodes beyond this many halflives contribute weight
# < 0.001 (2^{-10} ≈ 0.001) and are dropped before computing stats.
# Outputs are unchanged within float precision; the cutoff just avoids
# iterating over arbitrarily old history.
EFFECTIVE_WINDOW_HALFLIVES = 10


@dataclass
class DeathAwareRollingState(EstimatorState):
    """Minimal bookkeeping. Stats recompute from events each call."""

    def to_dict(self) -> dict:
        return {"n_completed": self.n_completed, "n_attempts": self.n_attempts}

    @classmethod
    def from_dict(cls, d: dict) -> "DeathAwareRollingState":
        return cls(
            n_completed=d["n_completed"],
            n_attempts=d["n_attempts"],
        )


EstimatorState.register_state("death_aware_rolling", DeathAwareRollingState)


def _resolve_halflife(params: dict | None) -> float:
    if not params or "halflife" not in params:
        return float(DEFAULT_HALFLIFE)
    raw = params["halflife"]
    try:
        v = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"halflife must be a number, got {raw!r}") from exc
    if v < 1.0 or v > 200.0:
        raise ValueError(f"halflife must be in [1, 200], got {v}")
    return v


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
                float(DEFAULT_HALFLIFE), 1.0, 200.0, 1.0,
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
