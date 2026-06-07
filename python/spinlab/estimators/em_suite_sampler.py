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

Exception: alpha=0 uses additive accumulation (new = old + driver) so its
slot is the uniform all-time mean — see ema_step.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import random

from spinlab.estimators import Estimator, EstimatorState
from spinlab.models import (
    DEFAULT_DEATH_PENALTY_MS,
    AttemptRecord,
    Estimate,
    EventAttempt,
    ModelOutput,
)

# Decay-rate grid (locked, strictly ascending). Endpoints are anchors:
# alpha=0.0 is the ZERO-DECAY anchor: uniform (equal-weight) all-time mean,
# handled specially in ema_step. alpha=1.0 is the goldfish anchor (last
# attempt only).
ALPHA_GRID: tuple[float, ...] = (
    0.0, 0.01, 0.02, 0.03, 0.05, 0.1, 0.2, 0.3, 0.5, 1.0,
)

# Default decay pair for the single headline scalar (the segment table's
# "Episode Time" and the greedy allocator's ranking number). fast=0.2 is
# ~5-attempt current skill; slow=0.05 is a ~20-attempt baseline. A named,
# tunable modeling default — NOT a magic pair. The scalar uses the no-slope
# sample(0) value, so only the fast rate affects it; the pair is kept whole so
# the same default feeds slope-aware diagnostics unchanged.
DEFAULT_ALPHA_FAST = 0.2
DEFAULT_ALPHA_SLOW = 0.05
DEFAULT_FAST_IDX = ALPHA_GRID.index(DEFAULT_ALPHA_FAST)
DEFAULT_SLOW_IDX = ALPHA_GRID.index(DEFAULT_ALPHA_SLOW)

# Draw-pool size per segment, per outcome. Sized to cover several effective
# windows of the slowest recency-weighted rate (alpha=0.01 ~ 100-attempt
# memory). Two SEPARATE pools (success, death) so a death-heavy segment never
# starves the success pool. Bump if a segment needs a deeper success history
# than this; it is a one-line change with no structural impact.
POOL_SIZE = 300

# Inner-loop cap for a single simulated episode. A draw that never survives
# within this many attempts returns None (non-converged) rather than a fudge
# value, per the no-silent-fallback principle. Reachable only at near-certain
# death; normal segments survive in a handful of attempts.
MAX_ATTEMPTS_PER_EPISODE = 100


def _append_capped(pool: list[float], value: float) -> list[float]:
    """Return a new list with `value` appended, keeping only the last POOL_SIZE
    entries (oldest dropped first). Immutable to match process_event's
    new-state-per-call convention."""
    return (pool + [value])[-POOL_SIZE:]


def draw_from_pool(
    pool: list[float], alpha: float, rng: "random.Random",
) -> float | None:
    """Draw one value from `pool`, recency-weighted by `alpha`.

    Weight on the entry of age k (k=0 is the newest, the last element) is
    proportional to alpha*(1-alpha)^k, matching the EMA's decay. alpha=0
    gives uniform weights (zero decay); alpha=1 puts all mass on the newest.
    Returns None for an empty pool (callers gate on non-empty pools first).
    """
    n = len(pool)
    if n == 0:
        return None
    if alpha <= 0.0:
        weights = [1.0] * n
    else:
        # Index j has age (n-1-j): the last element (j=n-1) has age 0.
        weights = [alpha * (1.0 - alpha) ** (n - 1 - j) for j in range(n)]
    return rng.choices(pool, weights=weights, k=1)[0]


def ema_step(values: list[float], driver: float) -> list[float]:
    """Apply one EMA-style update to all alphas in parallel.

    For alpha > 0: new[i] = alpha * driver + (1 - alpha) * values[i]  (normalized EMA).
    For alpha == 0: new[i] = values[i] + driver  (ZERO DECAY = unbiased
        accumulation). Because the Sum accumulator is driven by the
        observation and the Denom by 1.0, this makes the alpha=0 slot a true
        uniform all-time mean (Sum/Denom = sum(obs)/count), not the degenerate
        frozen-at-zero cell the plain alpha=0 substitution would give.

    Used for both the Sum (driver = observation) and Denom (driver = 1.0)
    accumulators inside the normalized EMA. Lengths must match ALPHA_GRID;
    a mismatch is a programming error (silent zip truncation otherwise).
    """
    if len(values) != len(ALPHA_GRID):
        raise ValueError(
            f"values has length {len(values)}, expected {len(ALPHA_GRID)}",
        )
    return [
        (v + driver) if a == 0.0 else (a * driver + (1.0 - a) * v)
        for v, a in zip(values, ALPHA_GRID)
    ]


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
    success_time_pool: list[float] = field(default_factory=list)
    death_time_pool: list[float] = field(default_factory=list)
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
            "success_time_pool": list(self.success_time_pool),
            "death_time_pool": list(self.death_time_pool),
            "n_successes": self.n_successes,
            "n_deaths": self.n_deaths,
            "n_attempts_total": self.n_attempts_total,
            # Base-class counters — episode-level totals consumed by the
            # segments table (SegmentWithModel reads n_completed for the "Runs"
            # column). Pinned by test_serialization_roundtrips_base_counters
            # to prevent regressions like the old reset-to-zero-on-load bug.
            "n_completed": self.n_completed,
            "n_attempts": self.n_attempts,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SamplerState":
        state = cls(
            log_success_time_sums=list(d["log_success_time_sums"]),
            log_success_time_denoms=list(d["log_success_time_denoms"]),
            log_death_time_sums=list(d["log_death_time_sums"]),
            log_death_time_denoms=list(d["log_death_time_denoms"]),
            p_die_sums=list(d["p_die_sums"]),
            p_die_denoms=list(d["p_die_denoms"]),
            # .get() ONLY for pools and base counters: cross-version tolerance.
            # Pools: absent in pre-Task-2 persisted states; rebuild from events
            # on next replay. Base counters: absent in pre-n_completed-fix
            # states; default to 0 so old states route through rebuild_state
            # on next update (same behavior as before the fix).
            # Every other field stays direct-indexed so a malformed row surfaces.
            success_time_pool=list(d.get("success_time_pool", [])),
            death_time_pool=list(d.get("death_time_pool", [])),
            n_successes=d["n_successes"],
            n_deaths=d["n_deaths"],
            n_attempts_total=d["n_attempts_total"],
        )
        state.n_completed = d.get("n_completed", 0)
        state.n_attempts = d.get("n_attempts", 0)
        return state


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

    new_success_pool = state.success_time_pool
    new_death_pool = state.death_time_pool

    if is_death:
        new_log_death_sums = ema_step(state.log_death_time_sums, log_time)
        new_log_death_denoms = ema_step(state.log_death_time_denoms, 1.0)
        new_log_success_sums = state.log_success_time_sums
        new_log_success_denoms = state.log_success_time_denoms
        new_n_deaths = state.n_deaths + 1
        new_n_successes = state.n_successes
        new_death_pool = _append_capped(state.death_time_pool, float(event.time_ms))
    else:
        new_log_success_sums = ema_step(state.log_success_time_sums, log_time)
        new_log_success_denoms = ema_step(state.log_success_time_denoms, 1.0)
        new_log_death_sums = state.log_death_time_sums
        new_log_death_denoms = state.log_death_time_denoms
        new_n_successes = state.n_successes + 1
        new_n_deaths = state.n_deaths
        new_success_pool = _append_capped(state.success_time_pool, float(event.time_ms))

    return SamplerState(
        log_success_time_sums=new_log_success_sums,
        log_success_time_denoms=new_log_success_denoms,
        log_death_time_sums=new_log_death_sums,
        log_death_time_denoms=new_log_death_denoms,
        p_die_sums=new_p_die_sums,
        p_die_denoms=new_p_die_denoms,
        success_time_pool=new_success_pool,
        death_time_pool=new_death_pool,
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


def gate_passes(state: SamplerState) -> bool:
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

    Returns None when the prediction gate fails, or when either EMA is
    None (insufficient data for that alpha). The α=0.0 anchor accumulates
    data via additive accumulation (zero-decay uniform mean) and is a valid
    slow-index reference once observations arrive.

    Each slope is E_fast − E_slow, in log-space for times and logit-space
    for p_die. The logit values are clamped to [LOGIT_EPS, 1−LOGIT_EPS] to
    defend against same-outcome streaks that drive p toward 0 or 1.
    """
    if not gate_passes(state):
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
    if not gate_passes(state):
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


def expected_episode_time_scalar(state: SamplerState) -> float | None:
    """The single headline expected-episode-time (ms) for a segment, or None
    below the prediction gate. Closed-form mean, no trend slide (sample(0)),
    at the default fast rate — the variance-free number for the table and the
    greedy allocator.

    Note: with apply_slope=False, slow_idx is inert (no slope is computed), so
    only DEFAULT_FAST_IDX affects the value. DEFAULT_SLOW_IDX is passed for API
    consistency with the slope-aware path, which the same default feeds later."""
    return expected_episode_time_ms(
        state, DEFAULT_FAST_IDX, DEFAULT_SLOW_IDX, apply_slope=False,
    )


def sample_episode(
    state: SamplerState, fast_idx: int, slow_idx: int, k: int = 0,
    *, rng: "random.Random",
    reload_penalty_ms: int = DEFAULT_DEATH_PENALTY_MS,
) -> float | None:
    """Draw one episode time (ms): bootstrap-with-slide.

    Repeatedly draw an attempt — died with probability p, else survived —
    drawing the attempt's gameplay time from the matching recency-weighted
    pool, until the first survival. Sum gameplay times + reload_penalty_ms per
    death. k>0 slides p (logit space) and the per-draw times (log space) by
    k * the (alpha_fast, alpha_slow) trend slopes.

    Returns None when the prediction gate fails, when either pool is empty,
    or when the draw does not survive within MAX_ATTEMPTS_PER_EPISODE.

    Gating note (intentional asymmetry with the closed-form scalar): the scalar
    (expected_episode_time_ms) refuses to compute when p >= 1 - LOGIT_EPS
    because the geometric *mean* diverges as p -> 1. This sampler has no such
    guard — at near-certain death it lets the empirical draw speak and instead
    bounds work via MAX_ATTEMPTS_PER_EPISODE (returning None only if it never
    survives in that many tries). So in the high-p corner the scalar may be None
    while a draw still returns a (large) number. That is deliberate: a divergent
    mean is dishonest, but an individual sampled episode is still well-defined.
    Corollary: mean(many sample_episode draws) is NOT expected_episode_time_scalar
    — the latter is a closed-form geometric mean of log-time; the former samples
    the raw empirical distribution. Do not use one as a regression check on the
    other.
    """
    if not gate_passes(state):
        return None
    # Post-gate (>=2 deaths and >=2 successes) both pools are non-empty in normal
    # flow; this guard is load-bearing only for a cross-version state deserialized
    # with empty pools (counters restored, pools default []) before events replay.
    if not state.success_time_pool or not state.death_time_pool:
        return None
    p_fast = state.p_die_ema(fast_idx)
    if p_fast is None:
        return None

    if k != 0:
        slopes = trend_signal_slopes(state, fast_idx, slow_idx)
        if slopes is None:
            return None
        slope_log_success, slope_log_death, slope_logit_p = slopes
        p = _logistic(_logit(p_fast) + k * slope_logit_p)
        slide_success = math.exp(k * slope_log_success)
        slide_death = math.exp(k * slope_log_death)
    else:
        p = p_fast
        slide_success = 1.0
        slide_death = 1.0

    alpha_fast = ALPHA_GRID[fast_idx]
    episode_ms = 0.0
    for _ in range(MAX_ATTEMPTS_PER_EPISODE):
        if rng.random() < p:  # died
            d = draw_from_pool(state.death_time_pool, alpha_fast, rng)
            assert d is not None  # both pools guarded non-empty at entry
            episode_ms += d * slide_death + reload_penalty_ms
        else:  # survived -> episode ends
            s = draw_from_pool(state.success_time_pool, alpha_fast, rng)
            assert s is not None  # both pools guarded non-empty at entry
            episode_ms += s * slide_success
            return episode_ms
    return None  # never survived within the cap


def replay_with_history(
    events: list[EventAttempt],
) -> tuple[SamplerState, dict[str, list[list[float | None]]]]:
    """Replay events and emit a per-snapshot history of the three quantities.

    Returns (final_state, history) where history is a dict with three keys:
    `p_die`, `log_success_time`, `log_death_time`. Each maps to a 2D list
    of shape ``[n_alphas][n_snapshots]`` with `n_snapshots = len(events) + 1`.
    Snapshot 0 is the empty initial state (all None); snapshot k > 0 is the
    state after processing event k-1. None marks an EMA that has no data yet
    (denominator == 0) at that snapshot. Times are stored as log(time_ms);
    the frontend exponentiates for display.

    Invalidated events still advance the snapshot count (their snapshot is
    identical to the prior one) so the snapshot index matches event-time
    order in the segment's event log.
    """
    n_alphas = len(ALPHA_GRID)
    n_snapshots = len(events) + 1
    p_die_history: list[list[float | None]] = [
        [None] * n_snapshots for _ in range(n_alphas)
    ]
    log_success_history: list[list[float | None]] = [
        [None] * n_snapshots for _ in range(n_alphas)
    ]
    log_death_history: list[list[float | None]] = [
        [None] * n_snapshots for _ in range(n_alphas)
    ]

    def snapshot(state: SamplerState, snap_idx: int) -> None:
        for i in range(n_alphas):
            p_die_history[i][snap_idx] = state.p_die_ema(i)
            log_success_history[i][snap_idx] = state.log_success_time_ema(i)
            log_death_history[i][snap_idx] = state.log_death_time_ema(i)

    state = SamplerState()
    snapshot(state, 0)
    for k, event in enumerate(events, start=1):
        state = process_event(state, event)
        snapshot(state, k)
    return state, {
        "p_die": p_die_history,
        "log_success_time": log_success_history,
        "log_death_time": log_death_history,
    }


def build_slope_matrices(
    state: SamplerState,
) -> dict[str, list[list[float | None]]]:
    """Three 10x10 upper-triangular slope matrices, one per quantity.

    Returns a dict with `slope_log_success`, `slope_log_death`,
    `slope_logit_p` keys. Each is a 10x10 list; cell [fast][slow] is the
    `E_fast - E_slow` value in the quantity's working space (log for times,
    logit for p_die). Cells where the prediction gate fails, where either
    EMA is undefined, or where fast_idx <= slow_idx are None. Thin wrapper
    around `trend_signal_slopes`; no math change.
    """
    n = len(ALPHA_GRID)
    s_success: list[list[float | None]] = [[None] * n for _ in range(n)]
    s_death: list[list[float | None]] = [[None] * n for _ in range(n)]
    s_logit_p: list[list[float | None]] = [[None] * n for _ in range(n)]
    for fast_idx in range(n):
        for slow_idx in range(fast_idx):
            slopes = trend_signal_slopes(state, fast_idx, slow_idx)
            if slopes is None:
                continue
            s_success[fast_idx][slow_idx] = slopes[0]
            s_death[fast_idx][slow_idx] = slopes[1]
            s_logit_p[fast_idx][slow_idx] = slopes[2]
    return {
        "slope_log_success": s_success,
        "slope_log_death": s_death,
        "slope_logit_p": s_logit_p,
    }


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
    when an alpha hasn't accumulated sufficient observations yet,
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


class EmSuiteSamplerEstimator(Estimator):
    """ABC adapter — the sole surviving model after the Plan 2 purge.

    ModelOutput.total carries the closed-form headline scalar
    (expected_ms / ms_per_attempt) at the DEFAULT_ALPHA_FAST rate with no
    trend slide — the number shown in the segment table and used by the greedy
    allocator. clean and extras remain unset pending the "Success Attempt"
    distribution work in Spec #2.

    The full per-segment prediction matrix is still served via a dedicated
    /em-suite-matrix route, which is independent of this scalar.

    The scheduler and ``/api/model`` route instantiate this class directly;
    there is no name-keyed registry indirection any more.
    """

    name = "em_suite_sampler"
    display_name = "EMA-Suite Sampler"

    def model_output(  # type: ignore[override]
        self, state: SamplerState, all_attempts: list[AttemptRecord],
        params: dict | None = None,
        events: list[EventAttempt] | None = None,
    ) -> ModelOutput:
        scalar = expected_episode_time_scalar(state)
        # Practice gain: expected now minus expected after one trend-slide step,
        # at the default alpha pair. Mirrors live_view.practice_gain_ms exactly.
        # None when either side is None (gate fails / slope ungated).
        slid = expected_episode_time_ms(
            state, DEFAULT_FAST_IDX, DEFAULT_SLOW_IDX, apply_slope=True,
        )
        practice_gain = (
            scalar - slid if (scalar is not None and slid is not None) else None
        )
        # Floor = running-min CLEAN clear so far (the final-success time with no
        # deaths) — the unreachable-without-a-perfect-run target. min over
        # completed attempts' clean_tail_ms; None until a clean clear exists.
        # (NB: the table's "Best" column is gold_ms, a separate metric — the two
        # may overlap; the Best/Floor/Expected/Room redesign reconciles them.)
        floor = min(
            (a.clean_tail_ms for a in all_attempts
             if a.completed and a.clean_tail_ms is not None),
            default=None,
        )
        # total carries the headline scalar in BOTH expected_ms (table) and
        # ms_per_attempt (greedy allocator). Note: ms_per_attempt here is the
        # absolute expected episode time, NOT an improvement slope (the other
        # trend estimators, being removed in the model purge, used it as a
        # rate); greedy just needs a rank-able magnitude. clean stays unmodeled
        # in Plan 1 — the "Success Attempt" distribution lands with the UI work
        # (Spec #2).
        total = Estimate(
            expected_ms=scalar, ms_per_attempt=scalar, floor_ms=floor,
        )
        clean = Estimate(expected_ms=None, ms_per_attempt=None, floor_ms=None)
        return ModelOutput(
            total=total, clean=clean, extras=None,
            practice_gain_ms=practice_gain,
        )

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
        # these are the fields the scheduler reads generically.
        state.n_completed = sum(1 for a in attempts if a.completed)
        state.n_attempts = len(attempts)
        return state
