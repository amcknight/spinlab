"""Greedy allocator: picks segment with highest marginal return."""
from __future__ import annotations

import random

from spinlab.allocators import Allocator, SegmentWithModel, register_allocator


@register_allocator
class GreedyAllocator(Allocator):
    name = "greedy"

    def pick_next(self, segment_states: list[SegmentWithModel]) -> str | None:
        if not segment_states:
            return None
        # Random tiebreaker when marginal returns are equal (e.g. all 0.0)
        best_mr = max(s.marginal_return for s in segment_states)
        tied = [s for s in segment_states if s.marginal_return == best_mr]
        return random.choice(tied).segment_id

    def peek_next_n(self, segment_states: list[SegmentWithModel], n: int) -> list[str]:
        # Shuffle first so ties are broken randomly, then stable-sort by MR
        shuffled = list(segment_states)
        random.shuffle(shuffled)
        shuffled.sort(key=lambda s: s.marginal_return, reverse=True)
        return [s.segment_id for s in shuffled[:n]]
