"""Estimator abstract base class and registry."""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from spinlab.db import Database
    from spinlab.models import AttemptRecord, ModelOutput


@dataclass
class ParamDef:
    """Describes a tunable estimator parameter."""
    name: str
    display_name: str
    default: float
    min_val: float
    max_val: float
    step: float
    description: str

    def to_dict(self) -> dict:
        return {
            "name": self.name, "display_name": self.display_name,
            "default": self.default, "min": self.min_val, "max": self.max_val,
            "step": self.step, "description": self.description,
        }


@dataclass
class EstimatorState(ABC):
    """Base class for estimator-specific state."""

    @classmethod
    def register_state(cls, name: str, state_cls: type["EstimatorState"]) -> None:
        cls._state_classes[name] = state_cls

    @classmethod
    def deserialize(cls, estimator_name: str, state_json: str) -> "EstimatorState":
        """Deserialize state JSON for a named estimator."""
        state_cls = cls._state_classes.get(estimator_name)
        if state_cls is None:
            raise ValueError(f"No state class for estimator: {estimator_name}")
        return state_cls.from_dict(json.loads(state_json))

    @abstractmethod
    def to_dict(self) -> dict:
        ...

    @classmethod
    @abstractmethod
    def from_dict(cls, d: dict) -> "EstimatorState":
        ...


# Class-level registry — set after class body to avoid dataclass field issues
EstimatorState._state_classes: dict[str, type[EstimatorState]] = {}


class Estimator(ABC):
    """Abstract estimator that tracks per-split performance."""

    name: str
    display_name: str = ""

    def declared_params(self) -> list["ParamDef"]:
        """Tunable params with metadata. Default: no params."""
        return []

    @abstractmethod
    def init_state(
        self, first_attempt: "AttemptRecord", priors: dict,
        params: dict | None = None,
    ) -> EstimatorState:
        """Initialize state from the first completed attempt."""
        ...

    @abstractmethod
    def process_attempt(
        self,
        state: EstimatorState,
        new_attempt: "AttemptRecord",
        all_attempts: list["AttemptRecord"],
        params: dict | None = None,
    ) -> EstimatorState:
        """Process one attempt. Uses new_attempt and/or all_attempts as needed."""
        ...

    @abstractmethod
    def model_output(
        self, state: EstimatorState, all_attempts: list["AttemptRecord"]
    ) -> "ModelOutput":
        """Produce standardized ModelOutput from current state."""
        ...

    @abstractmethod
    def rebuild_state(
        self, attempts: list["AttemptRecord"],
        params: dict | None = None,
    ) -> EstimatorState:
        """Rebuild state by replaying all attempts."""
        ...

    def get_priors(self, db: "Database", game_id: str) -> dict:
        """Return population priors for init_state. Default: no priors."""
        return {}


# Registry: name -> Estimator class
_ESTIMATOR_REGISTRY: dict[str, type[Estimator]] = {}


def register_estimator(cls: type[Estimator]) -> type[Estimator]:
    """Decorator to register an estimator class."""
    _ESTIMATOR_REGISTRY[cls.name] = cls
    return cls


def get_estimator(name: str) -> Estimator:
    """Instantiate an estimator by name."""
    if name not in _ESTIMATOR_REGISTRY:
        raise ValueError(
            f"Unknown estimator: {name!r}. "
            f"Available: {list(_ESTIMATOR_REGISTRY.keys())}"
        )
    return _ESTIMATOR_REGISTRY[name]()


def list_estimators() -> list[str]:
    """Return list of registered estimator names."""
    return list(_ESTIMATOR_REGISTRY.keys())
