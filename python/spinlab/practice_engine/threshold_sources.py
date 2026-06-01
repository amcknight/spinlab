"""Threshold-source helpers — produce per-segment cumulative thresholds for target_paced.

v0 ships two:
  thresholds_from_user        — caller passes per-segment cumulative splits explicitly.
  thresholds_from_gold_default — dev convenience: cumulative-sum of per-segment golds.

Future sources (PB-of-full-runs, WR-anchored, best-recent-N) are one-function additions.
"""
from __future__ import annotations

import numpy as np


def thresholds_from_user(
    seg_ids: list[str],
    cum_splits_ms: dict[str, int],
) -> np.ndarray:
    """User-entered per-segment cumulative split thresholds.

    Returns array shape (K,) of cumulative ms, one per segment in seg_ids order.
    KeyError if any seg_id has no entry in cum_splits_ms.
    """
    return np.array([cum_splits_ms[s] for s in seg_ids], dtype=np.float64)


def thresholds_from_gold_default(
    seg_ids: list[str],
    golds_ms: dict[str, int],
) -> np.ndarray:
    """Cumulative sum of per-segment golds — dashboard "fill from gold" default.

    Returns array shape (K,) of cumulative-gold ms.
    KeyError if any seg_id has no gold entry.
    """
    per_segment = np.array([golds_ms[s] for s in seg_ids], dtype=np.float64)
    return np.cumsum(per_segment)
