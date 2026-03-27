"""Greedy allocator: picks segment with highest ms_per_attempt from selected model."""
from __future__ import annotations

import random

from spinlab.allocators import Allocator, SegmentWithModel, register_allocator


def _score(s: SegmentWithModel) -> float:
    out = s.model_outputs.get(s.selected_model)
    if out is None:
        return 0.0
    return out.ms_per_attempt


@register_allocator
class GreedyAllocator(Allocator):
    name = "greedy"

    def pick_next(self, segment_states: list[SegmentWithModel]) -> str | None:
        if not segment_states:
            return None
        best = max(_score(s) for s in segment_states)
        tied = [s for s in segment_states if _score(s) == best]
        return random.choice(tied).segment_id

    def peek_next_n(self, segment_states: list[SegmentWithModel], n: int) -> list[str]:
        shuffled = list(segment_states)
        random.shuffle(shuffled)
        shuffled.sort(key=_score, reverse=True)
        return [s.segment_id for s in shuffled[:n]]
