"""Round robin allocator: cycles through segments in stable order."""
from __future__ import annotations

from spinlab.allocators import Allocator, SegmentWithModel, register_allocator


@register_allocator
class RoundRobinAllocator(Allocator):
    name = "round_robin"

    def __init__(self) -> None:
        self._index = 0

    def pick_next(self, segment_states: list[SegmentWithModel]) -> str | None:
        if not segment_states:
            return None
        idx = self._index % len(segment_states)
        self._index += 1
        return segment_states[idx].segment_id

    def peek_next_n(self, segment_states: list[SegmentWithModel], n: int) -> list[str]:
        if not segment_states:
            return []
        result = []
        for i in range(min(n, len(segment_states))):
            idx = (self._index + i) % len(segment_states)
            result.append(segment_states[idx].segment_id)
        return result
