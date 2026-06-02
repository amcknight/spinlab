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
    MAX_ATTEMPTS_PER_EPISODE,
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
    # Segments that pass the counter-based gate but cannot be sampled (empty draw
    # pools, or p_die≈1 so no draw survives within MAX_ATTEMPTS_PER_EPISODE). They
    # are excluded from T (so the panel never sees a degenerate column) and recorded
    # here with a human reason rather than crashing the whole build.
    unsamplable: dict[str, str] = field(init=False)
    # Stable seg_id -> column-seed-offset mapping; once assigned, never reused
    # so re-draws for the same segment are reproducible.
    _seed_offsets: dict[str, int] = field(init=False)
    _next_seed_offset: int = field(init=False)

    def __post_init__(self) -> None:
        self.T = np.zeros((self.N, 0), dtype=np.float64)
        self.seg_ids = []
        self.cost_ms = np.zeros((0,), dtype=np.float64)
        self.dirty = set()
        self.unsamplable = {}
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
        """Rebuild dirty columns, drop any now-ungated/unsamplable columns, add new ones."""
        if not self.dirty:
            return

        # Authoritative set of currently-gated candidate segments.
        gated = [s for s, st in self.sampler_states.items() if _gate_passes(st)]

        # The universe we've already classified = samplable columns + the gated
        # ones we excluded as unsamplable. If the gate-set itself changed, fall
        # back to a full rebuild (cheap at v0 N).
        prev_universe = set(self.seg_ids) | set(self.unsamplable)
        if set(gated) != prev_universe:
            self._rebuild_full(gated)
            return

        # Same gate-set; rebuild only dirty columns in place, re-checking
        # samplability. If a dirty column crosses the samplable/unsamplable line,
        # column membership changes — full rebuild handles the index shuffle.
        for seg_id in list(self.dirty):
            col = self._draw_column_impl(seg_id, k_param=0)
            was_samplable = seg_id in self.seg_ids
            now_samplable = col is not None
            if was_samplable != now_samplable:
                self._rebuild_full(gated)
                return
            if now_samplable:
                col_idx = self.seg_ids.index(seg_id)
                self.T[:, col_idx] = col
                self.cost_ms[col_idx] = float(col.mean())
            else:
                self.unsamplable[seg_id] = self._unsamplable_reason(seg_id)
        self.dirty.clear()

    def draw_column(self, seg_id: str, k_param: int) -> np.ndarray | None:
        """Draw N samples for a segment at the given k_param (0 or 1).

        Used by per-segment value attribution: pass k_param=1 to draw the
        "what if practiced once" column. Returns None when the segment is
        unsamplable at this k_param (no draw survives) — callers skip it rather
        than feed a degenerate column into an objective.
        """
        if seg_id not in self.sampler_states:
            raise KeyError(f"Unknown segment: {seg_id!r}")
        return self._draw_column_impl(seg_id, k_param=k_param)

    def _assign_seed_offset(self, seg_id: str) -> None:
        if seg_id in self._seed_offsets:
            return
        self._seed_offsets[seg_id] = self._next_seed_offset
        self._next_seed_offset += 1

    def _draw_column_impl(self, seg_id: str, k_param: int) -> np.ndarray | None:
        """Draw one N-length column. Returns None when EVERY draw came back None
        (the segment is unsamplable) — the caller excludes it. Partial None draws
        (rare) are filled with the column's mean so the row-sum stays honest."""
        state = self.sampler_states[seg_id]
        self._assign_seed_offset(seg_id)
        seed = self.rng_seed + self._seed_offsets[seg_id]
        rng = random.Random(seed)
        out = np.empty(self.N, dtype=np.float64)
        for n in range(self.N):
            v = sample_episode(state, DEFAULT_FAST_IDX, DEFAULT_SLOW_IDX, k=k_param, rng=rng)
            out[n] = v if v is not None else np.nan
        nan_mask = np.isnan(out)
        if nan_mask.all():
            return None
        if nan_mask.any():
            out[nan_mask] = out[~nan_mask].mean()
        return out

    def _unsamplable_reason(self, seg_id: str) -> str:
        """Human-readable reason a gated segment couldn't be sampled."""
        st = self.sampler_states[seg_id]
        if not st.success_time_pool or not st.death_time_pool:
            return "gated but unsamplable: empty draw pools"
        return (
            "gated but unsamplable: no surviving draw in "
            f"{MAX_ATTEMPTS_PER_EPISODE} attempts (p_die near 1)"
        )

    def _rebuild_full(self, gated_seg_ids: list[str]) -> None:
        """Rebuild the matrix from scratch, partitioning gated candidates into
        samplable columns (→ T) and unsamplable (→ self.unsamplable)."""
        for seg_id in gated_seg_ids:
            self._assign_seed_offset(seg_id)
        self.unsamplable = {}
        samplable: list[tuple[str, np.ndarray]] = []
        for seg_id in gated_seg_ids:
            col = self._draw_column_impl(seg_id, k_param=0)
            if col is None:
                self.unsamplable[seg_id] = self._unsamplable_reason(seg_id)
                continue
            samplable.append((seg_id, col))
        self.seg_ids = [s for s, _ in samplable]
        K = len(samplable)
        self.T = np.zeros((self.N, K), dtype=np.float64)
        self.cost_ms = np.zeros(K, dtype=np.float64)
        for k, (_, col) in enumerate(samplable):
            self.T[:, k] = col
            self.cost_ms[k] = float(col.mean())
        self.dirty.clear()
