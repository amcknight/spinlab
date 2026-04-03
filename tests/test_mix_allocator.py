"""Tests for MixAllocator weighted dispatch."""
import pytest
from unittest.mock import MagicMock
from spinlab.allocators import SegmentWithModel
from spinlab.allocators.greedy import GreedyAllocator
from spinlab.allocators.random import RandomAllocator
from spinlab.allocators.round_robin import RoundRobinAllocator
from spinlab.models import Estimate, ModelOutput
from tests.factories import make_segment_with_model


class TestMixAllocator:
    def test_single_allocator_100_percent(self):
        from spinlab.allocators.mix import MixAllocator
        greedy = GreedyAllocator()
        mix = MixAllocator(entries=[(greedy, 100)])
        segments = [make_segment_with_model("a", 50.0), make_segment_with_model("b", 100.0)]
        assert mix.pick_next(segments) == "b"

    def test_empty_segments_returns_none(self):
        from spinlab.allocators.mix import MixAllocator
        greedy = GreedyAllocator()
        mix = MixAllocator(entries=[(greedy, 100)])
        assert mix.pick_next([]) is None

    def test_empty_entries_returns_none(self):
        from spinlab.allocators.mix import MixAllocator
        mix = MixAllocator(entries=[])
        segments = [make_segment_with_model("a", 50.0)]
        assert mix.pick_next(segments) is None

    def test_zero_weight_allocator_never_picked(self):
        from spinlab.allocators.mix import MixAllocator
        greedy = GreedyAllocator()
        random = RandomAllocator()
        mix = MixAllocator(entries=[(greedy, 100), (random, 0)])
        segments = [make_segment_with_model("a", 50.0), make_segment_with_model("b", 100.0)]
        results = {mix.pick_next(segments) for _ in range(20)}
        assert results == {"b"}

    def test_weighted_distribution_over_many_picks(self):
        from spinlab.allocators.mix import MixAllocator
        alloc_a = MagicMock()
        alloc_a.pick_next = MagicMock(return_value="from_a")
        alloc_b = MagicMock()
        alloc_b.pick_next = MagicMock(return_value="from_b")
        mix = MixAllocator(entries=[(alloc_a, 80), (alloc_b, 20)])
        segments = [make_segment_with_model("x")]
        results = [mix.pick_next(segments) for _ in range(1000)]
        a_count = results.count("from_a")
        assert 650 < a_count < 950

    def test_round_robin_preserves_state_across_picks(self):
        from spinlab.allocators.mix import MixAllocator
        rr = RoundRobinAllocator()
        mix = MixAllocator(entries=[(rr, 100)])
        segments = [make_segment_with_model("a"), make_segment_with_model("b"), make_segment_with_model("c")]
        results = [mix.pick_next(segments) for _ in range(6)]
        assert results == ["a", "b", "c", "a", "b", "c"]
