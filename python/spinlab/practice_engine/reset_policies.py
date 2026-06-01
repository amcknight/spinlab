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
