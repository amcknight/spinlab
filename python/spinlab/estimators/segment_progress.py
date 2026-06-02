"""Segment-progress reducer — the 'am I improving on this segment?' signal.

Read-only reduction over a SamplerState. Reuses the sampler's α-suite: the
fast α (DEFAULT_FAST_IDX, ~last-5) is "Now" (current skill); the slow α
(DEFAULT_SLOW_IDX, ~last-20) is "Baseline". The signed gap between them is the
improvement signal. No modeling here — only reads of EMAs the sampler already
maintains, plus simple stats over the recent success pool.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass

from spinlab.estimators.em_suite_sampler import (
    ALPHA_GRID,
    DEFAULT_FAST_IDX,
    DEFAULT_SLOW_IDX,
    SamplerState,
    _gate_passes,
)

# Number of recent clears shown in the trend sparkline. Sized to ~one slow-α
# window (Baseline ≈ 20 attempts) so the line spans "now" back through the
# baseline the verdict compares against.
TREND_WINDOW = 20

# Effective sample size of the fast EMA: the number of recent observations that
# dominate its estimate. Derived directly from the decay rate: α=0.2 ↔ N≈5.
# Used as the slice size when estimating the noise band for the verdict.
_FAST_ALPHA_EFF_N: int = round(1.0 / ALPHA_GRID[DEFAULT_FAST_IDX])


@dataclass
class SegmentProgress:
    """'Am I improving?' summary for one segment. ms fields are None below gate.

    verdict ∈ {"faster", "holding", "slower", "not_ready"}. "holding" means the
    Now↔Baseline gap is within the standard error of the recent clears — i.e.
    indistinguishable from no change given the spread we've observed, NOT an
    arbitrary cutoff.
    """
    ready: bool
    verdict: str
    now_clear_ms: float | None        # recent (fast-α) expected clear time
    baseline_clear_ms: float | None   # baseline (slow-α) expected clear time
    death_rate: float                 # recent (fast-α) p_die; 0.0 below gate
    consistency_ms: float | None      # sample stdev of recent clears
    gap_to_gold_ms: float | None      # now_clear_ms − gold_ms (signed), or None
    pb_ms: float | None               # fastest clear in the pool
    trend_ms: list[float]             # recency-ordered recent clears (newest last)


def _ema_time_ms(state: SamplerState, idx: int) -> float | None:
    log_ms = state.log_success_time_ema(idx)
    return None if log_ms is None else math.exp(log_ms)


def segment_progress(state: SamplerState, gold_ms: int | None) -> SegmentProgress:
    if not _gate_passes(state):
        return SegmentProgress(
            ready=False, verdict="not_ready",
            now_clear_ms=None, baseline_clear_ms=None, death_rate=0.0,
            consistency_ms=None, gap_to_gold_ms=None, pb_ms=None, trend_ms=[],
        )

    now = _ema_time_ms(state, DEFAULT_FAST_IDX)
    baseline = _ema_time_ms(state, DEFAULT_SLOW_IDX)
    p_die = state.p_die_ema(DEFAULT_FAST_IDX)
    death_rate = float(p_die) if p_die is not None else 0.0

    recent = list(state.success_time_pool[-TREND_WINDOW:])
    consistency = float(statistics.stdev(recent)) if len(recent) >= 2 else None
    pb = float(min(state.success_time_pool)) if state.success_time_pool else None

    # Verdict: sign of (baseline_log − now_log), with a "holding" band equal to
    # the standard error of the log-clears within the fast-α effective window
    # (the last ~5 observations that dominate the fast EMA). Working in log space
    # is correct because the EMAs are stored in log space; the SE of the log-
    # clears is a principled uncertainty measure for the fast EMA estimate.
    # Inside the band the difference is indistinguishable from noise → "holding".
    now_log = state.log_success_time_ema(DEFAULT_FAST_IDX)
    baseline_log = state.log_success_time_ema(DEFAULT_SLOW_IDX)
    verdict = "holding"
    if now_log is not None and baseline_log is not None:
        delta_log = baseline_log - now_log  # positive = faster now than baseline
        noise_log = 0.0
        fast_slice = [math.log(x) for x in state.success_time_pool[-_FAST_ALPHA_EFF_N:]]
        if len(fast_slice) >= 2:
            noise_log = statistics.stdev(fast_slice) / math.sqrt(len(fast_slice))
        if delta_log > noise_log:
            verdict = "faster"
        elif delta_log < -noise_log:
            verdict = "slower"

    gap = (now - gold_ms) if (now is not None and gold_ms is not None) else None

    return SegmentProgress(
        ready=True, verdict=verdict,
        now_clear_ms=now, baseline_clear_ms=baseline, death_rate=death_rate,
        consistency_ms=consistency, gap_to_gold_ms=gap, pb_ms=pb,
        trend_ms=recent,
    )
