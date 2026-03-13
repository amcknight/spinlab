"""Greedy allocator: picks split with highest marginal return."""
from __future__ import annotations

import random

from spinlab.allocators import Allocator, SplitWithModel, register_allocator


@register_allocator
class GreedyAllocator(Allocator):
    name = "greedy"

    def pick_next(self, split_states: list[SplitWithModel]) -> str | None:
        if not split_states:
            return None
        # Random tiebreaker when marginal returns are equal (e.g. all 0.0)
        best_mr = max(s.marginal_return for s in split_states)
        tied = [s for s in split_states if s.marginal_return == best_mr]
        return random.choice(tied).split_id

    def peek_next_n(self, split_states: list[SplitWithModel], n: int) -> list[str]:
        # Shuffle first so ties are broken randomly, then stable-sort by MR
        shuffled = list(split_states)
        random.shuffle(shuffled)
        shuffled.sort(key=lambda s: s.marginal_return, reverse=True)
        return [s.split_id for s in shuffled[:n]]
