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
