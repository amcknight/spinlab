"""Least-played allocator: picks segment with fewest completed attempts."""
from __future__ import annotations

import random as _random

from spinlab.allocators import Allocator, SegmentWithModel, register_allocator


@register_allocator
class LeastPlayedAllocator(Allocator):
    name = "least_played"

    def pick_next(self, segment_states: list[SegmentWithModel]) -> str | None:
        if not segment_states:
            return None
        fewest = min(s.n_completed for s in segment_states)
        tied = [s for s in segment_states if s.n_completed == fewest]
        return _random.choice(tied).segment_id
