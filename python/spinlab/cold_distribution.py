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
