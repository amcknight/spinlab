"""MixAllocator: weighted random dispatch across multiple allocators."""
from __future__ import annotations

import random
from dataclasses import dataclass

from spinlab.allocators import Allocator, SegmentWithModel


@dataclass
class MixAllocator:
    """Holds (allocator, weight) pairs; dispatches each pick via weighted random."""

    entries: list[tuple[Allocator, int | float]]
    last_chosen_allocator: str | None = None

    def pick_next(self, segment_states: list[SegmentWithModel]) -> str | None:
        if not segment_states or not self.entries:
            self.last_chosen_allocator = None
            return None
        allocators, weights = zip(*self.entries)
        chosen = random.choices(allocators, weights=weights, k=1)[0]
        self.last_chosen_allocator = chosen.name
        return chosen.pick_next(segment_states)
