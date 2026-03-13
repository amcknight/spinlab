"""Tests for allocator implementations."""
import pytest
from spinlab.allocators import SplitWithModel
from spinlab.allocators.greedy import GreedyAllocator


def _make_split(split_id: str, marginal_return: float) -> SplitWithModel:
    return SplitWithModel(
        split_id=split_id,
        game_id="test",
        level_number=1,
        room_id=None,
        goal="normal",
        description="test",
        strat_version=1,
        reference_time_ms=None,
        state_path=None,
        active=True,
        marginal_return=marginal_return,
    )


class TestGreedyAllocator:
    def test_picks_highest_marginal_return(self):
        alloc = GreedyAllocator()
        splits = [_make_split("a", 0.05), _make_split("b", 0.10), _make_split("c", 0.02)]
        assert alloc.pick_next(splits) == "b"

    def test_peek_returns_sorted_order(self):
        alloc = GreedyAllocator()
        splits = [_make_split("a", 0.05), _make_split("b", 0.10), _make_split("c", 0.02)]
        result = alloc.peek_next_n(splits, 2)
        assert result == ["b", "a"]

    def test_empty_list_returns_none(self):
        alloc = GreedyAllocator()
        assert alloc.pick_next([]) is None

    def test_peek_empty_returns_empty(self):
        alloc = GreedyAllocator()
        assert alloc.peek_next_n([], 5) == []

    def test_peek_more_than_available(self):
        alloc = GreedyAllocator()
        splits = [_make_split("a", 0.05)]
        assert alloc.peek_next_n(splits, 5) == ["a"]


from spinlab.allocators.random import RandomAllocator
from spinlab.allocators.round_robin import RoundRobinAllocator


class TestRandomAllocator:
    def test_picks_from_available(self):
        alloc = RandomAllocator()
        splits = [_make_split("a", 0.0), _make_split("b", 0.0)]
        result = alloc.pick_next(splits)
        assert result in ("a", "b")

    def test_empty_returns_none(self):
        alloc = RandomAllocator()
        assert alloc.pick_next([]) is None

    def test_peek_no_replacement(self):
        alloc = RandomAllocator()
        splits = [_make_split("a", 0.0), _make_split("b", 0.0), _make_split("c", 0.0)]
        result = alloc.peek_next_n(splits, 3)
        assert len(result) == 3
        assert len(set(result)) == 3


class TestRoundRobinAllocator:
    def test_cycles_through_all(self):
        alloc = RoundRobinAllocator()
        splits = [_make_split("a", 0.0), _make_split("b", 0.0), _make_split("c", 0.0)]
        results = [alloc.pick_next(splits) for _ in range(6)]
        assert results == ["a", "b", "c", "a", "b", "c"]

    def test_empty_returns_none(self):
        alloc = RoundRobinAllocator()
        assert alloc.pick_next([]) is None

    def test_peek_returns_upcoming(self):
        alloc = RoundRobinAllocator()
        splits = [_make_split("a", 0.0), _make_split("b", 0.0), _make_split("c", 0.0)]
        result = alloc.peek_next_n(splits, 2)
        assert result == ["a", "b"]


def _make_split_with_ordinal(split_id: str, ordinal: int) -> SplitWithModel:
    return SplitWithModel(
        split_id=split_id,
        game_id="test",
        level_number=ordinal * 10,
        room_id=None,
        goal="normal",
        description=f"Split {split_id}",
        strat_version=1,
        reference_time_ms=None,
        state_path=None,
        active=True,
        marginal_return=0.0,
    )


class TestRoundRobinOrdinalOrder:
    def test_cycles_in_list_order(self):
        """Round Robin should iterate in the order splits are provided."""
        alloc = RoundRobinAllocator()
        splits = [
            _make_split_with_ordinal("c", 1),
            _make_split_with_ordinal("a", 2),
            _make_split_with_ordinal("b", 3),
        ]
        results = [alloc.pick_next(splits) for _ in range(3)]
        assert results == ["c", "a", "b"]
