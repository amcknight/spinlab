"""PracticeEngine — consumer-facing API over the rollout matrix.

Combines a RolloutMatrix with pluggable ResetPolicy + Objective functions to
produce: scalar objective values, total-time distributions, per-segment stats,
and per-segment value attributions (the §4 ranking primitive, in Task 7).
"""
from __future__ import annotations

from collections import Counter
from typing import Callable

import numpy as np

from spinlab.estimators.em_suite_sampler import SamplerState
from spinlab.practice_engine.rollout_matrix import RolloutMatrix
from spinlab.practice_engine.types import ResetMasks

ResetPolicy = Callable[..., ResetMasks]
Objective = Callable[[np.ndarray, ResetMasks, dict], float | None]

# Default histogram bin count for total_time_distribution. 30 strikes a balance
# between resolution and readability for typical N=10000 rollouts.
DEFAULT_HISTOGRAM_BIN_COUNT = 30


class PracticeEngine:
    """Holds the rollout matrix and exposes reductions over it."""

    def __init__(
        self,
        sampler_states: dict[str, SamplerState],
        N: int,
        rng_seed: int = 0,
    ) -> None:
        self.matrix = RolloutMatrix(
            sampler_states=sampler_states, N=N, rng_seed=rng_seed,
        )

    def invalidate(self, seg_id: str) -> None:
        """Mark a segment's column dirty. See RolloutMatrix.invalidate."""
        self.matrix.invalidate(seg_id)

    def evaluate(
        self,
        policy: ResetPolicy,
        threshold_kwargs: dict,
        objective: Objective,
        ctx: dict,
    ) -> dict:
        """Single objective evaluation. Returns {value, masks_summary}."""
        self.matrix.ensure_fresh()
        masks = policy(self.matrix.T, **threshold_kwargs)
        value = objective(self.matrix.T, masks, ctx)
        return {
            "value": value,
            "masks_summary": self._masks_summary(masks),
        }

    def total_time_distribution(
        self,
        policy: ResetPolicy,
        threshold_kwargs: dict,
        bin_count: int = DEFAULT_HISTOGRAM_BIN_COUNT,
    ) -> dict:
        """Histogram payload of finished wall_ms under the given policy."""
        self.matrix.ensure_fresh()
        masks = policy(self.matrix.T, **threshold_kwargs)
        finished_wall = masks.wall_ms[masks.finished]
        if finished_wall.size == 0:
            return {
                "bins": [], "counts": [],
                "mean": None, "median": None, "p10": None, "p90": None,
                "finished_count": 0,
            }
        counts, bin_edges = np.histogram(finished_wall, bins=bin_count)
        return {
            "bins": bin_edges.tolist(),
            "counts": counts.tolist(),
            "mean": float(finished_wall.mean()),
            "median": float(np.median(finished_wall)),
            "p10": float(np.quantile(finished_wall, 0.10)),
            "p90": float(np.quantile(finished_wall, 0.90)),
            "finished_count": int(finished_wall.size),
        }

    def column_summary(self, seg_id: str) -> dict:
        """Per-segment column stats for the dashboard table."""
        self.matrix.ensure_fresh()
        if seg_id not in self.matrix.seg_ids:
            raise KeyError(f"Unknown or ungated segment: {seg_id!r}")
        col_idx = self.matrix.seg_ids.index(seg_id)
        col = self.matrix.T[:, col_idx]
        swap_col = self.matrix.draw_column(seg_id, k_param=1)
        return {
            "seg_id": seg_id,
            "n": int(col.size),
            "mean": float(col.mean()),
            "p10": float(np.quantile(col, 0.10)),
            "p50": float(np.quantile(col, 0.50)),
            "p90": float(np.quantile(col, 0.90)),
            "e_sample_0_ms": float(col.mean()),
            "e_sample_1_ms": float(swap_col.mean()),
        }

    def _masks_summary(self, masks: ResetMasks) -> dict:
        """Compact summary of ResetMasks for the dashboard."""
        n = masks.finished.size
        if n == 0:
            return {"finished_pct": 0.0, "aborted_by_segment": {}}
        finished_pct = float(masks.finished.mean() * 100.0)
        aborted_at = masks.abort_at[~masks.finished]
        seg_ids = self.matrix.seg_ids
        aborted_by_segment: dict[str, int] = {}
        if aborted_at.size > 0:
            counter = Counter(int(a) for a in aborted_at if 0 <= int(a) < len(seg_ids))
            for col_idx, count in counter.items():
                aborted_by_segment[seg_ids[col_idx]] = count
        return {
            "finished_pct": finished_pct,
            "aborted_by_segment": aborted_by_segment,
        }
