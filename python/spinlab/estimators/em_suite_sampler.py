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

**Normalized EMA.** Each EMA is stored as a (Sum, Denom) pair, updated by

    new_S = alpha * obs + (1 - alpha) * S      (S_0 = 0)
    new_D = alpha       + (1 - alpha) * D      (D_0 = 0)
    EMA   = S / D                              (None when D == 0)

This is the finite-sample correction over the standard `new = α·obs + (1-α)·old`
update with a "seed at first observation" hack. The standard form implicitly
assumes a synthetic prior `X_0 = X_1`, which gives the first observation
weight `(1-α)^(N-1)` after N samples — ~96% for α=0.01 at N=5. The normalized
form divides by the actually-observed weight mass `1 − (1-α)^N` so the
weights on real observations sum to 1. Both converge to the same value as
N → ∞; the normalized form is materially more honest at small N.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from spinlab.estimators import (
    Estimator,
    EstimatorState,
    ParamDef,
    register_estimator,
)
from spinlab.models import (
    DEFAULT_DEATH_PENALTY_MS,
    AttemptRecord,
    Estimate,
    EventAttempt,
    ModelOutput,
)

# Decay-rate grid (locked, strictly ascending). Endpoints (0.0, 1.0) are
# sanity-check anchors that should look obviously broken on the matrix.
# Note: under the normalized scheme, α=0.0 produces D = 0 forever and so
# never yields a valid EMA — the cell will remain "—" no matter how much
# data lands. That's the honest reading of "never incorporate observations."
ALPHA_GRID: tuple[float, ...] = (
    0.0, 0.01, 0.02, 0.03, 0.05, 0.1, 0.2, 0.3, 0.5, 1.0,
)


def ema_step(values: list[float], driver: float) -> list[float]:
    """Apply one EMA-style update to all alphas in parallel.

    For each alpha: new[i] = alpha * driver + (1 - alpha) * values[i].

    Used for both the Sum (driver = observation) and Denom (driver = 1.0)
    accumulators inside the normalized EMA. Lengths must match ALPHA_GRID;
    a mismatch is a programming error (silent zip truncation otherwise).
    """
    if len(values) != len(ALPHA_GRID):
        raise ValueError(
            f"values has length {len(values)}, expected {len(ALPHA_GRID)}",
        )
    return [a * driver + (1.0 - a) * v for v, a in zip(values, ALPHA_GRID)]


def _ema(s: float, d: float) -> float | None:
    """Compute EMA from a (Sum, Denom) pair. None when D == 0 (no data yet)."""
    return None if d == 0.0 else s / d


@dataclass
class SamplerState(EstimatorState):
    """Per-segment EMA-suite state, normalized form.

    Each tracked quantity is two parallel arrays indexed by ALPHA_GRID
    position: a Sum accumulator and a Denom accumulator. The EMA is
    Sum / Denom; an unused alpha (D == 0) reads as None.

    For times we accumulate log(time_ms); for p_die we accumulate the
    Bernoulli outcome bit (1 if died else 0).

    n_successes / n_deaths / n_attempts_total drive the prediction-gate
    check (nil-until-2 of each kind).

    Inherits n_completed / n_attempts from EstimatorState (the scheduler
    reads those generically; do not redefine them here).
    """

    log_success_time_sums: list[float] = field(
        default_factory=lambda: [0.0] * len(ALPHA_GRID),
    )
    log_success_time_denoms: list[float] = field(
        default_factory=lambda: [0.0] * len(ALPHA_GRID),
    )
    log_death_time_sums: list[float] = field(
        default_factory=lambda: [0.0] * len(ALPHA_GRID),
    )
    log_death_time_denoms: list[float] = field(
        default_factory=lambda: [0.0] * len(ALPHA_GRID),
    )
    p_die_sums: list[float] = field(
        default_factory=lambda: [0.0] * len(ALPHA_GRID),
    )
    p_die_denoms: list[float] = field(
        default_factory=lambda: [0.0] * len(ALPHA_GRID),
    )
    n_successes: int = 0
    n_deaths: int = 0
    n_attempts_total: int = 0

    def log_success_time_ema(self, idx: int) -> float | None:
        return _ema(self.log_success_time_sums[idx], self.log_success_time_denoms[idx])

    def log_death_time_ema(self, idx: int) -> float | None:
        return _ema(self.log_death_time_sums[idx], self.log_death_time_denoms[idx])

    def p_die_ema(self, idx: int) -> float | None:
        return _ema(self.p_die_sums[idx], self.p_die_denoms[idx])

    def to_dict(self) -> dict:
        return {
            "log_success_time_sums": list(self.log_success_time_sums),
            "log_success_time_denoms": list(self.log_success_time_denoms),
            "log_death_time_sums": list(self.log_death_time_sums),
            "log_death_time_denoms": list(self.log_death_time_denoms),
            "p_die_sums": list(self.p_die_sums),
            "p_die_denoms": list(self.p_die_denoms),
            "n_successes": self.n_successes,
            "n_deaths": self.n_deaths,
            "n_attempts_total": self.n_attempts_total,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SamplerState":
        return cls(
            log_success_time_sums=list(d["log_success_time_sums"]),
            log_success_time_denoms=list(d["log_success_time_denoms"]),
            log_death_time_sums=list(d["log_death_time_sums"]),
            log_death_time_denoms=list(d["log_death_time_denoms"]),
            p_die_sums=list(d["p_die_sums"]),
            p_die_denoms=list(d["p_die_denoms"]),
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
    - Each EMA update advances BOTH the Sum and Denom accumulators.
    - n_attempts_total / n_successes / n_deaths advance accordingly.

    Returns a new state; does not mutate the input.
    """
    if event.invalidated:
        return state

    is_death = event.outcome.value == "died"
    outcome_bit = 1.0 if is_death else 0.0
    log_time = math.log(max(event.time_ms, 1))

    new_p_die_sums = ema_step(state.p_die_sums, outcome_bit)
    new_p_die_denoms = ema_step(state.p_die_denoms, 1.0)

    if is_death:
        new_log_death_sums = ema_step(state.log_death_time_sums, log_time)
        new_log_death_denoms = ema_step(state.log_death_time_denoms, 1.0)
        new_log_success_sums = state.log_success_time_sums
        new_log_success_denoms = state.log_success_time_denoms
        new_n_deaths = state.n_deaths + 1
        new_n_successes = state.n_successes
    else:
        new_log_success_sums = ema_step(state.log_success_time_sums, log_time)
        new_log_success_denoms = ema_step(state.log_success_time_denoms, 1.0)
        new_log_death_sums = state.log_death_time_sums
        new_log_death_denoms = state.log_death_time_denoms
        new_n_successes = state.n_successes + 1
        new_n_deaths = state.n_deaths

    return SamplerState(
        log_success_time_sums=new_log_success_sums,
        log_success_time_denoms=new_log_success_denoms,
        log_death_time_sums=new_log_death_sums,
        log_death_time_denoms=new_log_death_denoms,
        p_die_sums=new_p_die_sums,
        p_die_denoms=new_p_die_denoms,
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

    Returns None when the prediction gate fails, OR when either alpha is
    the α=0.0 anchor (its EMA never gains a denominator, so no slope is
    available against it).

    Each slope is E_fast − E_slow, in log-space for times and logit-space
    for p_die. The logit values are clamped to [LOGIT_EPS, 1−LOGIT_EPS] to
    defend against same-outcome streaks that drive p toward 0 or 1.
    """
    if not _gate_passes(state):
        return None
    s_fast = state.log_success_time_ema(fast_idx)
    s_slow = state.log_success_time_ema(slow_idx)
    d_fast = state.log_death_time_ema(fast_idx)
    d_slow = state.log_death_time_ema(slow_idx)
    p_fast = state.p_die_ema(fast_idx)
    p_slow = state.p_die_ema(slow_idx)
    if (s_fast is None or s_slow is None
            or d_fast is None or d_slow is None
            or p_fast is None or p_slow is None):
        return None
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

    Returns None when the prediction gate fails, when the chosen alpha has
    no observations yet (denom == 0), or when p is too close to 1 (the
    geometric mean diverges as p → 1).
    """
    if not _gate_passes(state):
        return None

    s_fast = state.log_success_time_ema(fast_idx)
    d_fast = state.log_death_time_ema(fast_idx)
    p_fast = state.p_die_ema(fast_idx)
    if s_fast is None or d_fast is None or p_fast is None:
        return None

    if apply_slope:
        slopes = trend_signal_slopes(state, fast_idx, slow_idx)
        if slopes is None:
            return None
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

    None cells appear when either the prediction gate fails (insufficient data),
    when an alpha hasn't accumulated any observations (α=0.0 forever),
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


@register_estimator
class EmSuiteSamplerEstimator(Estimator):
    """ABC adapter — registers the EMA-suite sampler with the estimator pipeline.

    v0: this estimator does NOT populate ModelOutput.total/clean
    (it doesn't drive the legacy expected_ms display in the existing UI).
    The per-segment matrix is served via a dedicated route. ModelOutput
    fields are kept None to make this explicit.

    Why register at all: rebuild_state is the canonical entry point for
    replaying historical events through the sampler — used by both the
    matrix endpoint and the offline replay script.
    """

    name = "em_suite_sampler"
    display_name = "EMA-Suite Sampler"

    def declared_params(self) -> list[ParamDef]:
        # No tunable params for v0; the alpha suite is fixed.
        return []

    def init_state(
        self, first_attempt: AttemptRecord, priors: dict,
        params: dict | None = None,
    ) -> SamplerState:
        return SamplerState()

    def process_attempt(  # type: ignore[override]
        self, state: SamplerState, new_attempt: AttemptRecord,
        all_attempts: list[AttemptRecord],
        params: dict | None = None,
        events: list[EventAttempt] | None = None,
    ) -> SamplerState:
        # Rebuild from full event list on every call. Matches the
        # death_aware_rolling pattern: state contains no history, all
        # computation is on the event log.
        if events is None:
            return state
        return self.rebuild_state(all_attempts, params=params, events=events)

    def model_output(  # type: ignore[override]
        self, state: SamplerState, all_attempts: list[AttemptRecord],
        params: dict | None = None,
        events: list[EventAttempt] | None = None,
    ) -> ModelOutput:
        none_estimate = Estimate(
            expected_ms=None, ms_per_attempt=None, floor_ms=None,
        )
        return ModelOutput(total=none_estimate, clean=none_estimate, extras=None)

    def rebuild_state(  # type: ignore[override]
        self, attempts: list[AttemptRecord],
        params: dict | None = None,
        events: list[EventAttempt] | None = None,
    ) -> SamplerState:
        # Event-level EMAs come from replaying events.
        state = SamplerState()
        if events is not None:
            for event in events:
                state = process_event(state, event)
        # Episode-level inherited counters come from the AttemptRecord list —
        # these are the fields the scheduler reads generically. Pattern matches
        # DeathAwareRollingEstimator.rebuild_state.
        state.n_completed = sum(1 for a in attempts if a.completed)
        state.n_attempts = len(attempts)
        return state
