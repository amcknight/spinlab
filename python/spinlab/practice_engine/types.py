"""Shared dataclasses for the practice simulation engine."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class ResetMasks:
    """Per-rollout outcome of applying a reset policy to a rollout matrix.

    Shapes (for a matrix of N rollouts):
      finished[N]  — bool; True when the rollout reached the end without aborting.
      abort_at[N]  — int; segment index where the abort triggered, or -1 if finished.
      wall_ms[N]   — float64; cumulative wall-clock through and including the
                     aborting segment (or full run total if finished).
    """
    finished: np.ndarray
    abort_at: np.ndarray
    wall_ms: np.ndarray


@dataclass
class PerSegmentValue:
    """Per-segment improvement under one more practice attempt.

    value           — baseline_objective − swap_to_k=1_objective, raw signed.
                      UI colors by sign per objective direction.
    value_per_second — value / cost_ms[i]; None if cost_ms is zero.
    e_sample_0_ms   — column mean before swap.
    e_sample_1_ms   — column mean after swap to k=1 draws.
    """
    seg_id: str
    value: float
    value_per_second: float | None
    e_sample_0_ms: float
    e_sample_1_ms: float
