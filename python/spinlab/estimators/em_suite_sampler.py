"""EMA-Suite Sampler — per-segment three-quantity sampler.

See docs/superpowers/specs/2026-05-30-em-suite-sampler-design.md.

Maintains a fixed suite of 10 exponential-moving-average decay rates for each
of three quantities per segment:
- p_die: Bernoulli outcome per attempt (proportion in [0, 1])
- log success_time: gameplay-ms log of successful attempts
- log death_time: gameplay-ms log of fatal attempts

Trend signal = E_fast − E_slow per quantity, in log-space for times and
logit-space for p_die. Predictions are closed-form mean of the geometric
process: E[episode_time] = success_time + (p/(1−p)) * (death_time + reload).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from spinlab.estimators import EstimatorState

# Decay-rate grid (locked, strictly ascending). Endpoints (0.0, 1.0) are
# sanity-check anchors that should look obviously broken on the matrix.
ALPHA_GRID: tuple[float, ...] = (
    0.0, 0.01, 0.02, 0.03, 0.05, 0.1, 0.2, 0.3, 0.5, 1.0,
)


def update_ema_array(
    values: list[float | None], observation: float,
) -> list[float | None]:
    """Apply one EMA update to all alphas in the suite, in parallel.

    For each alpha:
      - If existing value is None, seed at observation (no decay yet).
      - Else, value' = alpha * observation + (1 - alpha) * value.

    ``values`` MUST have length ``len(ALPHA_GRID)``; passing a shorter list
    is a programming error (results would be silently truncated by ``zip``).
    Returns a new list; does not mutate input.
    """
    if len(values) != len(ALPHA_GRID):
        raise ValueError(
            f"values has length {len(values)}, expected {len(ALPHA_GRID)}",
        )
    return [
        observation if v is None
        else (alpha * observation + (1.0 - alpha) * v)
        for v, alpha in zip(values, ALPHA_GRID)
    ]


@dataclass
class SamplerState(EstimatorState):
    """Per-segment EMA-suite state.

    Each EMA array is indexed by ALPHA_GRID position. Values are None until
    the segment observes its first attempt of the matching kind. Subsequent
    attempts apply the normal update rule (see update_ema_array).

    log_success_time_emas: EMA of log(gameplay_ms) for successful completions.
      Seeded on first success; None until then.
    log_death_time_emas: EMA of log(gameplay_ms) for fatal attempts.
      Seeded on first death; None until then.
    p_die_emas: EMA of Bernoulli outcome (1=death, 0=success) per attempt.
      Seeded on first attempt of any kind.

    n_successes / n_deaths / n_attempts_total drive the prediction-gate
    check (nil-until-2 of each).

    Inherits n_completed / n_attempts from EstimatorState (the scheduler
    reads those generically; do not redefine them here).
    """

    log_success_time_emas: list[float | None] = field(
        default_factory=lambda: [None] * len(ALPHA_GRID),
    )
    log_death_time_emas: list[float | None] = field(
        default_factory=lambda: [None] * len(ALPHA_GRID),
    )
    p_die_emas: list[float | None] = field(
        default_factory=lambda: [None] * len(ALPHA_GRID),
    )
    n_successes: int = 0
    n_deaths: int = 0
    n_attempts_total: int = 0

    def to_dict(self) -> dict:
        return {
            "log_success_time_emas": list(self.log_success_time_emas),
            "log_death_time_emas": list(self.log_death_time_emas),
            "p_die_emas": list(self.p_die_emas),
            "n_successes": self.n_successes,
            "n_deaths": self.n_deaths,
            "n_attempts_total": self.n_attempts_total,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SamplerState":
        return cls(
            log_success_time_emas=list(d["log_success_time_emas"]),
            log_death_time_emas=list(d["log_death_time_emas"]),
            p_die_emas=list(d["p_die_emas"]),
            n_successes=d["n_successes"],
            n_deaths=d["n_deaths"],
            n_attempts_total=d["n_attempts_total"],
        )


EstimatorState.register_state("em_suite_sampler", SamplerState)
