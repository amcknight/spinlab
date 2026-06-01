"""Rollout matrix — the backbone of the practice simulation engine.

T[N, K]: per-segment-per-rollout sample times. Columns are owned by segments;
when a segment's SamplerState mutates, its column gets marked dirty and is
rebuilt on the next ensure_fresh().
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

import numpy as np

from spinlab.estimators.em_suite_sampler import (
    DEFAULT_FAST_IDX,
    DEFAULT_SLOW_IDX,
    SamplerState,
    _gate_passes,
    sample_episode,
)


@dataclass
class RolloutMatrix:
    """Lazy, column-keyed rollout matrix.

    Construction notes:
      - sampler_states is a live mapping; new gated segments are auto-included
        on ensure_fresh().
      - N rollouts per column, fixed at construction (use a new RolloutMatrix
        if you need a different N).
      - rng_seed is global; each column uses random.Random(rng_seed + per-column-offset).
    """
    sampler_states: dict[str, SamplerState]
    N: int
    rng_seed: int

    T: np.ndarray = field(init=False)
    seg_ids: list[str] = field(init=False)
    cost_ms: np.ndarray = field(init=False)
    dirty: set[str] = field(init=False)
    # Stable seg_id -> column-seed-offset mapping; once assigned, never reused
    # so re-draws for the same segment are reproducible.
    _seed_offsets: dict[str, int] = field(init=False)
    _next_seed_offset: int = field(init=False)

    def __post_init__(self) -> None:
        self.T = np.zeros((self.N, 0), dtype=np.float64)
        self.seg_ids = []
        self.cost_ms = np.zeros((0,), dtype=np.float64)
        self.dirty = set()
        self._seed_offsets = {}
        self._next_seed_offset = 0
        # Mark all currently-gated states dirty so the first ensure_fresh()
        # builds the matrix from scratch.
        for seg_id, state in self.sampler_states.items():
            if _gate_passes(state):
                self._assign_seed_offset(seg_id)
                self.dirty.add(seg_id)

    def invalidate(self, seg_id: str) -> None:
        """Mark a column dirty. No-op if the segment is unknown."""
        if seg_id in self.sampler_states:
            self.dirty.add(seg_id)

    def ensure_fresh(self) -> None:
        """Rebuild dirty columns, drop any now-ungated columns, add new ones."""
        if not self.dirty:
            return

        # Authoritative set of currently-gated segments.
        gated = [s for s, st in self.sampler_states.items() if _gate_passes(st)]

        current_set = set(self.seg_ids)
        new_set = set(gated)
        if current_set != new_set:
            self._rebuild_full(gated)
            return

        # Same column set; rebuild only dirty columns in place.
        for seg_id in list(self.dirty):
            col_idx = self.seg_ids.index(seg_id)
            new_col = self._draw_column_impl(seg_id, k_param=0)
            self.T[:, col_idx] = new_col
            self.cost_ms[col_idx] = float(new_col.mean())
        self.dirty.clear()

    def draw_column(self, seg_id: str, k_param: int) -> np.ndarray:
        """Draw N samples for a segment at the given k_param (0 or 1).

        Used by per-segment value attribution: pass k_param=1 to draw the
        "what if practiced once" column.
        """
        if seg_id not in self.sampler_states:
            raise KeyError(f"Unknown segment: {seg_id!r}")
        return self._draw_column_impl(seg_id, k_param=k_param)

    def _assign_seed_offset(self, seg_id: str) -> None:
        if seg_id in self._seed_offsets:
            return
        self._seed_offsets[seg_id] = self._next_seed_offset
        self._next_seed_offset += 1

    def _draw_column_impl(self, seg_id: str, k_param: int) -> np.ndarray:
        state = self.sampler_states[seg_id]
        self._assign_seed_offset(seg_id)
        seed = self.rng_seed + self._seed_offsets[seg_id]
        rng = random.Random(seed)
        out = np.empty(self.N, dtype=np.float64)
        for n in range(self.N):
            v = sample_episode(state, DEFAULT_FAST_IDX, DEFAULT_SLOW_IDX, k=k_param, rng=rng)
            out[n] = v if v is not None else np.nan
        # If any draws came back None, replace nans with the column's nan-mean.
        # These are rare on a gated state; a fully-None column means the gate
        # logic is bugged — surface it.
        if np.isnan(out).any():
            non_nan = out[~np.isnan(out)]
            if non_nan.size == 0:
                raise RuntimeError(
                    f"All {self.N} sample_episode draws returned None for {seg_id!r}; "
                    f"likely a gate logic bug."
                )
            out[np.isnan(out)] = non_nan.mean()
        return out

    def _rebuild_full(self, gated_seg_ids: list[str]) -> None:
        """Rebuild the matrix from scratch for the given segment set."""
        for seg_id in gated_seg_ids:
            self._assign_seed_offset(seg_id)
        self.seg_ids = list(gated_seg_ids)
        K = len(self.seg_ids)
        self.T = np.zeros((self.N, K), dtype=np.float64)
        self.cost_ms = np.zeros(K, dtype=np.float64)
        for k, seg_id in enumerate(self.seg_ids):
            col = self._draw_column_impl(seg_id, k_param=0)
            self.T[:, k] = col
            self.cost_ms[k] = float(col.mean())
        self.dirty.clear()
