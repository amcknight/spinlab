"""Tests for allocator implementations."""
import pytest
from spinlab.allocators import SegmentWithModel
from spinlab.allocators.greedy import GreedyAllocator
from spinlab.models import Estimate, ModelOutput


def _make_segment(segment_id: str, ms_per_attempt: float = 0.0) -> SegmentWithModel:
    out = ModelOutput(
        total=Estimate(expected_ms=10000.0, ms_per_attempt=ms_per_attempt, floor_ms=8000.0),
        clean=Estimate(expected_ms=10000.0, ms_per_attempt=ms_per_attempt, floor_ms=8000.0),
    )
    return SegmentWithModel(
        segment_id=segment_id, game_id="test", level_number=1,
        start_type="level_enter", start_ordinal=0,
        end_type="level_exit", end_ordinal=0,
        description="test", strat_version=1, state_path=None, active=True,
        model_outputs={"kalman": out}, selected_model="kalman",
    )


class TestGreedyAllocator:
    def test_picks_highest_ms_per_attempt(self):
        alloc = GreedyAllocator()
        segments = [_make_segment("a", 50.0), _make_segment("b", 100.0), _make_segment("c", 20.0)]
        assert alloc.pick_next(segments) == "b"

    def test_empty_list_returns_none(self):
        alloc = GreedyAllocator()
        assert alloc.pick_next([]) is None


from spinlab.allocators.random import RandomAllocator
from spinlab.allocators.round_robin import RoundRobinAllocator


class TestRandomAllocator:
    def test_picks_from_available(self):
        alloc = RandomAllocator()
        segments = [_make_segment("a", 0.0), _make_segment("b", 0.0)]
        result = alloc.pick_next(segments)
        assert result in ("a", "b")

    def test_empty_returns_none(self):
        alloc = RandomAllocator()
        assert alloc.pick_next([]) is None


class TestRoundRobinAllocator:
    def test_cycles_through_all(self):
        alloc = RoundRobinAllocator()
        segments = [_make_segment("a", 0.0), _make_segment("b", 0.0), _make_segment("c", 0.0)]
        results = [alloc.pick_next(segments) for _ in range(6)]
        assert results == ["a", "b", "c", "a", "b", "c"]

    def test_empty_returns_none(self):
        alloc = RoundRobinAllocator()
        assert alloc.pick_next([]) is None


def _make_segment_with_ordinal(segment_id: str, ordinal: int) -> SegmentWithModel:
    return SegmentWithModel(
        segment_id=segment_id, game_id="test", level_number=ordinal * 10,
        start_type="level_enter", start_ordinal=ordinal,
        end_type="level_exit", end_ordinal=ordinal,
        description=f"Segment {segment_id}", strat_version=1,
        state_path=None, active=True,
    )


class TestRoundRobinOrdinalOrder:
    def test_cycles_in_list_order(self):
        """Round Robin should iterate in the order segments are provided."""
        alloc = RoundRobinAllocator()
        segments = [
            _make_segment_with_ordinal("c", 1),
            _make_segment_with_ordinal("a", 2),
            _make_segment_with_ordinal("b", 3),
        ]
        results = [alloc.pick_next(segments) for _ in range(3)]
        assert results == ["c", "a", "b"]
