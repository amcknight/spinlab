"""Random allocator: uniform random selection."""
from __future__ import annotations

import random as _random

from spinlab.allocators import Allocator, SegmentWithModel, register_allocator


@register_allocator
class RandomAllocator(Allocator):
    name = "random"

    def pick_next(self, segment_states: list[SegmentWithModel]) -> str | None:
        if not segment_states:
            return None
        return _random.choice(segment_states).segment_id
