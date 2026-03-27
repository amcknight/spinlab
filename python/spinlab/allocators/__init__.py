"""Allocator abstract base class, SegmentWithModel, and registry."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from spinlab.models import ModelOutput


@dataclass
class SegmentWithModel:
    """Segment metadata combined with all estimator outputs."""

    segment_id: str
    game_id: str
    level_number: int
    start_type: str
    start_ordinal: int
    end_type: str
    end_ordinal: int
    description: str
    strat_version: int
    state_path: str | None
    active: bool
    # Multi-model output
    model_outputs: dict[str, ModelOutput] = field(default_factory=dict)
    selected_model: str = "kalman"
    n_completed: int = 0
    n_attempts: int = 0
    gold_ms: int | None = None
    clean_gold_ms: int | None = None


class Allocator(ABC):
    """Abstract allocator that picks next segment to practice."""

    name: str

    @abstractmethod
    def pick_next(self, segment_states: list[SegmentWithModel]) -> str | None:
        """Pick next segment_id to practice, or None if list is empty."""
        ...

    @abstractmethod
    def peek_next_n(self, segment_states: list[SegmentWithModel], n: int) -> list[str]:
        """Preview next N segment_ids without side effects."""
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
