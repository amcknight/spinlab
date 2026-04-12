"""Allocator abstract base class, SegmentWithModel, and registry."""
from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from spinlab.models import ModelOutput

if TYPE_CHECKING:
    from spinlab.db import Database

logger = logging.getLogger(__name__)


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

    @classmethod
    def load_all(
        cls,
        db: "Database",
        game_id: str,
        selected_model: str = "kalman",
    ) -> list["SegmentWithModel"]:
        """Load all segments for a game with model outputs, golds, and stats."""
        rows = db.get_all_segments_with_model(game_id)
        all_model_states = db.load_all_model_states_for_game(game_id)
        golds = db.compute_golds(game_id)

        segments = []
        for row in rows:
            segment_id = row["id"]
            model_outputs: dict[str, ModelOutput] = {}
            n_completed = 0
            n_attempts = 0

            for sr in all_model_states.get(segment_id, []):
                if sr["output_json"]:
                    try:
                        out = ModelOutput.from_dict(json.loads(sr["output_json"]))
                        model_outputs[sr["estimator"]] = out
                    except (json.JSONDecodeError, KeyError):
                        logger.warning(
                            "Failed to deserialize model output for segment=%s estimator=%s",
                            segment_id, sr["estimator"],
                        )
                if sr["state_json"]:
                    try:
                        sd = json.loads(sr["state_json"])
                        nc = sd.get("n_completed", 0)
                        na = sd.get("n_attempts", 0)
                        if nc > n_completed:
                            n_completed = nc
                            n_attempts = na
                    except (json.JSONDecodeError, KeyError):
                        logger.warning(
                            "Failed to deserialize model state for segment=%s estimator=%s",
                            segment_id, sr["estimator"],
                        )

            gold_data = golds.get(segment_id, {})

            segments.append(cls(
                segment_id=segment_id,
                game_id=row["game_id"],
                level_number=row["level_number"],
                start_type=row["start_type"],
                start_ordinal=row["start_ordinal"],
                end_type=row["end_type"],
                end_ordinal=row["end_ordinal"],
                description=row["description"],
                strat_version=row["strat_version"],
                state_path=row.get("state_path"),
                active=bool(row["active"]),
                model_outputs=model_outputs,
                selected_model=selected_model,
                n_completed=n_completed,
                n_attempts=n_attempts,
                gold_ms=gold_data.get("gold_ms"),
                clean_gold_ms=gold_data.get("clean_gold_ms"),
            ))
        return segments


class Allocator(ABC):
    """Abstract allocator that picks next segment to practice."""

    name: str

    @abstractmethod
    def pick_next(self, segment_states: list[SegmentWithModel]) -> str | None:
        """Pick next segment_id to practice, or None if list is empty."""
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


def _register_all():
    """Import all allocator modules to trigger @register_allocator decorators."""
    from . import greedy, least_played, random, round_robin

_register_all()
