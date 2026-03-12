"""Random allocator: uniform random selection."""
from __future__ import annotations

import random as _random

from spinlab.allocators import Allocator, SplitWithModel, register_allocator


@register_allocator
class RandomAllocator(Allocator):
    name = "random"

    def pick_next(self, split_states: list[SplitWithModel]) -> str | None:
        if not split_states:
            return None
        return _random.choice(split_states).split_id

    def peek_next_n(self, split_states: list[SplitWithModel], n: int) -> list[str]:
        sample_size = min(n, len(split_states))
        return [s.split_id for s in _random.sample(split_states, sample_size)]
