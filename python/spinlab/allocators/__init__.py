"""Allocator abstract base class, SplitWithModel, and registry."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from spinlab.estimators import EstimatorState


@dataclass
class SplitWithModel:
    """Split metadata combined with estimator output."""

    # Split metadata (from splits table)
    split_id: str
    game_id: str
    level_number: int
    room_id: int | None
    goal: str
    description: str
    strat_version: int
    reference_time_ms: int | None
    state_path: str | None
    active: bool
    # Estimator output
    estimator_state: EstimatorState | None = None
    marginal_return: float = 0.0
    drift_info: dict = field(default_factory=dict)
    n_completed: int = 0
    n_attempts: int = 0
    gold_ms: int | None = None


class Allocator(ABC):
    """Abstract allocator that picks next split to practice."""

    name: str

    @abstractmethod
    def pick_next(self, split_states: list[SplitWithModel]) -> str | None:
        """Pick next split_id to practice, or None if list is empty."""
        ...

    @abstractmethod
    def peek_next_n(self, split_states: list[SplitWithModel], n: int) -> list[str]:
        """Preview next N split_ids without side effects."""
        ...


# Registry: name -> Allocator class
_ALLOCATOR_REGISTRY: dict[str, type[Allocator]] = {}


def register_allocator(cls: type[Allocator]) -> type[Allocator]:
    """Decorator to register an allocator class."""
    _ALLOCATOR_REGISTRY[cls.name] = cls
    return cls


def get_allocator(name: str) -> Allocator:
    """Instantiate an allocator by name."""
    if name not in _ALLOCATOR_REGISTRY:
        raise ValueError(
            f"Unknown allocator: {name!r}. "
            f"Available: {list(_ALLOCATOR_REGISTRY.keys())}"
        )
    return _ALLOCATOR_REGISTRY[name]()


def list_allocators() -> list[str]:
    """Return list of registered allocator names."""
    return list(_ALLOCATOR_REGISTRY.keys())
