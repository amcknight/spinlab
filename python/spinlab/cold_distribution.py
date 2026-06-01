"""Per-segment cold-attempt distribution computation.

Backs the "Cold distribution" panel on the segment-detail page: both
the histogram view (raw deaths/completions per bin) and the hazard
view (deaths_w / at_risk_w per bin, added in Phase 1).

Operates on a flat list of cold EventAttempts — episode-level
aggregation is irrelevant here (every attempt is its own risk
timeline). Caller is responsible for the cold filter (is_hot=False);
this module trusts its input.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from spinlab.api_schemas import ColdBin, ColdDistribution
from spinlab.models import AttemptOutcome, EventAttempt

# Maximum bin count. 20 matches screen-width comfort at typical viewport
# widths; above this, bars are too thin to read.
MAX_BINS = 20

# Minimum bin count. Below 5 the chart degenerates into a quantile
# summary and loses its shape-as-distribution affordance.
MIN_BINS = 5

# Truncation horizon in halflives. At 5*halflife back, an attempt's
# weight is 2^-5 ≈ 3%, below the noise floor of the binning. Matches
# EFFECTIVE_WINDOW_HALFLIVES in spinlab.estimators.death_aware_rolling.
EFFECTIVE_WINDOW_HALFLIVES = 5

# X-axis upper-edge rounding. One-second rounding gives clean axis
# labels without manual tick configuration.
HI_ROUND_MS = 1000


@dataclass
class _EpisodeAccum:
    """Per-episode accumulator for cold-distribution episode-level aggregates.

    Used internally by compute_cold_distribution to track each episode's
    representative weight (latest-event weight) and a had-death flag for
    the p_die_per_attempt computation.
    """
    weight: float
    had_death: bool


def _compute_attempt_weights(n: int, halflife: int) -> list[float]:
    """Per-attempt exponential decay weights, chronological order.

    weights[i] = 2 ** (-(n - 1 - i) / halflife)

    The most-recent attempt (index n-1) has weight 1.0. An attempt one
    halflife back has weight 0.5. Mirrors _episode_helpers._compute_weights
    but operates at the attempt level (not episode level) since cold-
    filtered analysis treats each attempt as its own risk timeline.
    """
    return [2.0 ** (-(n - 1 - i) / halflife) for i in range(n)]


def _bin_count_for(n: int) -> int:
    """Adaptive bin count via the square-root rule, clamped to [MIN_BINS, MAX_BINS]."""
    if n <= 0:
        return MIN_BINS
    return min(MAX_BINS, max(MIN_BINS, math.ceil(math.sqrt(n))))


def compute_cold_distribution(
    cold_events: list[EventAttempt], halflife: int,
) -> ColdDistribution:
    """Bin + summarize cold attempts for the segment-detail panel.

    Caller filters to is_hot=False BEFORE calling this. The function
    does not re-filter; passing any hot event skews the result. Empty
    input is disallowed (caller substitutes None at the route layer).
    """
    if not cold_events:
        raise ValueError("compute_cold_distribution requires non-empty cold_events")

    # 1. Truncate to last 5*halflife events. Recency-decay weights past
    #    this horizon are < 3% — negligible vs. binning noise.
    horizon = EFFECTIVE_WINDOW_HALFLIVES * halflife
    truncated = cold_events[-horizon:] if len(cold_events) > horizon else list(cold_events)
    n = len(truncated)

    # 2. Per-attempt decay weights.
    weights = _compute_attempt_weights(n=n, halflife=halflife)

    # 3. Bin count from sqrt rule.
    bin_count = _bin_count_for(n=n)

    # 4. X-axis range: lo=0; hi = ceil(max_time/HI_ROUND_MS)*HI_ROUND_MS.
    max_ms = max(ev.time_ms for ev in truncated)
    hi = max(HI_ROUND_MS, ((max_ms + HI_ROUND_MS - 1) // HI_ROUND_MS) * HI_ROUND_MS)
    lo = 0
    bin_width = (hi - lo) / bin_count

    # 5. Initialize bins.
    bins: list[ColdBin] = [
        ColdBin(
            lo_ms=lo + i * bin_width,
            hi_ms=lo + (i + 1) * bin_width,
            n_deaths=0,
            n_completions=0,
        )
        for i in range(bin_count)
    ]

    # 6. Walk events, fill bin counts and weighted aggregates.
    def bin_idx(t: int) -> int:
        if bin_width == 0:
            return 0
        idx = int((t - lo) // bin_width)
        if idx >= bin_count:
            idx = bin_count - 1
        if idx < 0:
            idx = 0
        return idx

    sum_w_d = 0.0   # weighted death count
    sum_w_c = 0.0   # weighted completion count
    sum_wt_d = 0.0  # weighted sum of death times
    sum_wt_c = 0.0  # weighted sum of completion times
    # Completion-only σ + log-moment accumulators back the Normal/Lognormal
    # overlays on the histogram. Death times don't get a parametric fit —
    # see ColdDistribution docstring.
    sum_wtt_c = 0.0       # weighted sum of completion times squared (for σ)
    sum_w_log_c = 0.0     # weight of cold completions with t>0
    sum_wlogt_c = 0.0     # Σ w * ln(t)
    sum_wlogtlogt_c = 0.0  # Σ w * ln(t)²

    # Hazard accumulators: per-bin weighted death count, and all
    # (time_ms, weight) pairs for at_risk_w computation below.
    deaths_w_per_bin: list[float] = [0.0] * bin_count
    event_weights_at_time: list[tuple[int, float]] = []

    # Episode-level: per-episode "had at least one death" indicator.
    # p_die_per_attempt = weighted fraction of episodes with a death.
    # The "weight" used per episode is the weight of the most-recent
    # event in that episode (a reasonable proxy for "episode recency").
    episodes_seen: dict[str, _EpisodeAccum] = {}

    for ev, w in zip(truncated, weights):
        idx = bin_idx(ev.time_ms)
        ep_entry = episodes_seen.setdefault(ev.episode_id, _EpisodeAccum(weight=w, had_death=False))
        # Chronological order; later overrides earlier.
        ep_entry.weight = w
        event_weights_at_time.append((ev.time_ms, w))
        if ev.outcome == AttemptOutcome.DIED:
            bins[idx].n_deaths += 1
            sum_w_d += w
            sum_wt_d += w * ev.time_ms
            deaths_w_per_bin[idx] += w
            ep_entry.had_death = True
        elif ev.outcome == AttemptOutcome.SURVIVED:
            bins[idx].n_completions += 1
            sum_w_c += w
            sum_wt_c += w * ev.time_ms
            sum_wtt_c += w * ev.time_ms * ev.time_ms
            if ev.time_ms > 0:
                lt = math.log(ev.time_ms)
                sum_w_log_c += w
                sum_wlogt_c += w * lt
                sum_wlogtlogt_c += w * lt * lt

    # Hazard: at_risk_w[i] = sum of weights of all events whose time_ms
    # >= lo_i (the attempt was still "alive" at the start of this bin).
    # hazard[i] = deaths_w_per_bin[i] / at_risk_w[i], or None when 0.
    for i, b in enumerate(bins):
        at_risk_w = sum(w for t, w in event_weights_at_time if t >= b.lo_ms)
        b.at_risk_w = at_risk_w
        if at_risk_w > 0:
            b.hazard = deaths_w_per_bin[i] / at_risk_w
        else:
            b.hazard = None

    # 7. Aggregates.
    mu_d_ms = sum_wt_d / sum_w_d if sum_w_d > 0 else None
    mu_c_ms = sum_wt_c / sum_w_c if sum_w_c > 0 else None

    # Completion-only σ + log-moments (see ColdDistribution docstring for why
    # the death side gets no parametric fit).
    sigma_c_ms = _weighted_std(sum_w_c, sum_wt_c, sum_wtt_c)
    mu_log_c, sigma_log_c = _weighted_log_moments(sum_w_log_c, sum_wlogt_c, sum_wlogtlogt_c)

    total_w_life = sum_w_d + sum_w_c
    p_die_per_life = sum_w_d / total_w_life if total_w_life > 0 else None

    total_ep_w = sum(e.weight for e in episodes_seen.values())
    had_death_w = sum(e.weight for e in episodes_seen.values() if e.had_death)
    p_die_per_attempt = had_death_w / total_ep_w if total_ep_w > 0 else None

    return ColdDistribution(
        bins=bins, n_cold_attempts=n,
        mu_d_ms=mu_d_ms, mu_c_ms=mu_c_ms,
        sigma_c_ms=sigma_c_ms,
        mu_log_c=mu_log_c, sigma_log_c=sigma_log_c,
        p_die_per_attempt=p_die_per_attempt, p_die_per_life=p_die_per_life,
        halflife=halflife,
    )


def _weighted_std(
    sum_w: float, sum_wt: float, sum_wtt: float,
) -> float | None:
    """Weighted population standard deviation, or None when undefined.

    Uses the population (biased) form: σ² = E[t²] − μ². With a single
    weighted sample the variance is 0; we still return 0.0 so callers can
    detect "no spread" rather than "no sample." Returns None only when
    there were no contributing events (sum_w == 0).
    """
    if sum_w <= 0:
        return None
    mu = sum_wt / sum_w
    var = sum_wtt / sum_w - mu * mu
    if var < 0:  # float roundoff near degenerate samples
        var = 0.0
    return math.sqrt(var)


def _weighted_log_moments(
    sum_w_log: float, sum_wlogt: float, sum_wlogtlogt: float,
) -> tuple[float | None, float | None]:
    """Weighted (mean, std) of ln(t) for the cold outcome.

    Returns (None, None) when no positive-t events contributed. Inputs are
    pre-accumulated from events with t > 0 only; t ≤ 0 events are silently
    skipped at fill time since the lognormal model has no support there.
    """
    if sum_w_log <= 0:
        return (None, None)
    mu_log = sum_wlogt / sum_w_log
    var_log = sum_wlogtlogt / sum_w_log - mu_log * mu_log
    if var_log < 0:
        var_log = 0.0
    return (mu_log, math.sqrt(var_log))
