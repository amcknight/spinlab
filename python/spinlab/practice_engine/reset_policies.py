"""Reset policies — pure functions over the rollout matrix.

Each policy: (T[N,K], **kwargs) -> ResetMasks
"""
from __future__ import annotations

import numpy as np

from spinlab.practice_engine.types import ResetMasks


def no_reset(T: np.ndarray) -> ResetMasks:
    """Trivial policy: every rollout finishes. wall_ms is the full row sum."""
    N = T.shape[0]
    return ResetMasks(
        finished=np.ones(N, dtype=bool),
        abort_at=np.full(N, -1, dtype=np.int32),
        wall_ms=T.sum(axis=1),
    )


def target_paced(
    T: np.ndarray,
    threshold_cum_ms: np.ndarray | None,
    slack: float = 0.0,
) -> ResetMasks:
    """Abort the first time cumulative time exceeds threshold_cum_ms[k] * (1+slack).

    If threshold_cum_ms is None, behaves identically to no_reset.
    """
    if threshold_cum_ms is None:
        return no_reset(T)
    N, K = T.shape
    cum = T.cumsum(axis=1)
    threshold = threshold_cum_ms * (1.0 + slack)
    over = cum > threshold[None, :]
    any_over = over.any(axis=1)
    abort_at = np.where(any_over, over.argmax(axis=1), -1).astype(np.int32)
    finished = np.logical_not(any_over)
    # safe_abort gives a valid index for the gather; for finished rows we
    # gather the last segment's cumulative time (full row sum).
    safe_abort = np.where(any_over, abort_at, K - 1)
    wall_ms = cum[np.arange(N), safe_abort]
    return ResetMasks(finished=finished, abort_at=abort_at, wall_ms=wall_ms)
