"""Greedy allocator: picks split with highest marginal return."""
from __future__ import annotations

from spinlab.allocators import Allocator, SplitWithModel, register_allocator


@register_allocator
class GreedyAllocator(Allocator):
    name = "greedy"

    def pick_next(self, split_states: list[SplitWithModel]) -> str | None:
        if not split_states:
            return None
        best = max(split_states, key=lambda s: s.marginal_return)
        return best.split_id

    def peek_next_n(self, split_states: list[SplitWithModel], n: int) -> list[str]:
        sorted_splits = sorted(split_states, key=lambda s: s.marginal_return, reverse=True)
        return [s.split_id for s in sorted_splits[:n]]
