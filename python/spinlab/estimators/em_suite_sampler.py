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
