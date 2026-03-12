"""Round robin allocator: cycles through splits in stable order."""
from __future__ import annotations

from spinlab.allocators import Allocator, SplitWithModel, register_allocator


@register_allocator
class RoundRobinAllocator(Allocator):
    name = "round_robin"

    def __init__(self) -> None:
        self._index = 0

    def pick_next(self, split_states: list[SplitWithModel]) -> str | None:
        if not split_states:
            return None
        idx = self._index % len(split_states)
        self._index += 1
        return split_states[idx].split_id

    def peek_next_n(self, split_states: list[SplitWithModel], n: int) -> list[str]:
        if not split_states:
            return []
        result = []
        for i in range(min(n, len(split_states))):
            idx = (self._index + i) % len(split_states)
            result.append(split_states[idx].split_id)
        return result
