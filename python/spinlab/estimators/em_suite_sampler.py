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

import math
from dataclasses import dataclass, field

from spinlab.estimators import EstimatorState
from spinlab.models import DEFAULT_DEATH_PENALTY_MS, EventAttempt

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


def process_event(state: SamplerState, event: EventAttempt) -> SamplerState:
    """Update sampler state with one observed event.

    - Invalidated events are no-ops (returned state == input state).
    - p_die updates on every attempt (outcome_bit = 1 if died else 0).
    - The matching time EMA updates only on attempts of that outcome.
    - n_attempts_total / n_successes / n_deaths advance accordingly.

    Returns a new state; does not mutate the input.
    """
    if event.invalidated:
        return state

    is_death = event.outcome.value == "died"
    outcome_bit = 1.0 if is_death else 0.0
    log_time = math.log(max(event.time_ms, 1))

    new_p_die = update_ema_array(state.p_die_emas, outcome_bit)
    if is_death:
        new_log_death = update_ema_array(state.log_death_time_emas, log_time)
        new_log_success = state.log_success_time_emas
        new_n_deaths = state.n_deaths + 1
        new_n_successes = state.n_successes
    else:
        new_log_success = update_ema_array(state.log_success_time_emas, log_time)
        new_log_death = state.log_death_time_emas
        new_n_successes = state.n_successes + 1
        new_n_deaths = state.n_deaths

    return SamplerState(
        log_success_time_emas=new_log_success,
        log_death_time_emas=new_log_death,
        p_die_emas=new_p_die,
        n_successes=new_n_successes,
        n_deaths=new_n_deaths,
        n_attempts_total=state.n_attempts_total + 1,
    )


# Numerical defense for logit at the [0, 1] edges from same-outcome streaks.
LOGIT_EPS = 1e-6


def _logit(p: float) -> float:
    clamped = max(LOGIT_EPS, min(1.0 - LOGIT_EPS, p))
    return math.log(clamped / (1.0 - clamped))


def _logistic(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def _gate_passes(state: SamplerState) -> bool:
    """Prediction gate: nil-until-2 of each outcome and overall."""
    return (
        state.n_successes >= 2
        and state.n_deaths >= 2
        and state.n_attempts_total >= 2
    )


def trend_signal_slopes(
    state: SamplerState, fast_idx: int, slow_idx: int,
) -> tuple[float, float, float] | None:
    """Compute (slope_log_success, slope_log_death, slope_logit_p_die).

    Returns None when the prediction gate fails (nil-until-2 of each kind).

    Each slope is E_fast − E_slow, in log-space for times and logit-space
    for p_die. The logit values are clamped to [LOGIT_EPS, 1−LOGIT_EPS] to
    defend against same-outcome streaks that drive p toward 0 or 1.
    """
    if not _gate_passes(state):
        return None
    s_fast = state.log_success_time_emas[fast_idx]
    s_slow = state.log_success_time_emas[slow_idx]
    d_fast = state.log_death_time_emas[fast_idx]
    d_slow = state.log_death_time_emas[slow_idx]
    p_fast = state.p_die_emas[fast_idx]
    p_slow = state.p_die_emas[slow_idx]
    # Gate guarantees ≥ 2 of each outcome, so the matching EMAs are seeded.
    assert s_fast is not None and s_slow is not None
    assert d_fast is not None and d_slow is not None
    assert p_fast is not None and p_slow is not None
    return (
        s_fast - s_slow,
        d_fast - d_slow,
        _logit(p_fast) - _logit(p_slow),
    )


def expected_episode_time_ms(
    state: SamplerState, fast_idx: int, slow_idx: int,
    *, apply_slope: bool,
    reload_penalty_ms: int = DEFAULT_DEATH_PENALTY_MS,
) -> float | None:
    """Closed-form mean of the geometric episode-time process.

    Formula:
      E[episode] = success_time + (p / (1 - p)) * (death_time + reload)

    Where:
      success_time = exp(log_E_fast_success [+ slope_log_success if apply_slope])
      death_time   = exp(log_E_fast_death   [+ slope_log_death   if apply_slope])
      p            = p_E_fast               [shifted in logit space if apply_slope]

    Returns None when the prediction gate fails or when p is too close to 1
    (the geometric mean diverges as p → 1).
    """
    if not _gate_passes(state):
        return None

    s_fast = state.log_success_time_emas[fast_idx]
    d_fast = state.log_death_time_emas[fast_idx]
    p_fast = state.p_die_emas[fast_idx]
    assert s_fast is not None and d_fast is not None and p_fast is not None

    if apply_slope:
        slopes = trend_signal_slopes(state, fast_idx, slow_idx)
        assert slopes is not None  # gate already passed
        slope_log_success, slope_log_death, slope_logit_p_die = slopes
        success_time = math.exp(s_fast + slope_log_success)
        death_time = math.exp(d_fast + slope_log_death)
        p = _logistic(_logit(p_fast) + slope_logit_p_die)
    else:
        success_time = math.exp(s_fast)
        death_time = math.exp(d_fast)
        p = p_fast

    if p >= 1.0 - LOGIT_EPS:
        return None  # geometric mean diverges
    return success_time + (p / (1.0 - p)) * (death_time + reload_penalty_ms)


def build_matrix(
    state: SamplerState, *,
    reload_penalty_ms: int = DEFAULT_DEATH_PENALTY_MS,
) -> dict:
    """Compute the full per-segment prediction matrix.

    Returns a dict with:
      - alpha_grid: list[float]    — the suite's alpha values in order
      - baseline: list[float|None] — sample(0) per alpha (no slope; one number per row)
      - matrix: list[list[float|None]] — sample(1) per (fast_idx, slow_idx).
        Upper-triangular: cell [fast][slow] is non-None iff fast > slow.

    None cells appear when either the prediction gate fails (insufficient data)
    or when fast_idx <= slow_idx.
    """
    n = len(ALPHA_GRID)
    baseline: list[float | None] = []
    matrix: list[list[float | None]] = []
    for fast_idx in range(n):
        baseline.append(
            expected_episode_time_ms(
                state, fast_idx, fast_idx, apply_slope=False,
                reload_penalty_ms=reload_penalty_ms,
            )
        )
        row: list[float | None] = []
        for slow_idx in range(n):
            if slow_idx >= fast_idx:
                row.append(None)
            else:
                row.append(
                    expected_episode_time_ms(
                        state, fast_idx, slow_idx, apply_slope=True,
                        reload_penalty_ms=reload_penalty_ms,
                    )
                )
        matrix.append(row)
    return {
        "alpha_grid": list(ALPHA_GRID),
        "baseline": baseline,
        "matrix": matrix,
    }
