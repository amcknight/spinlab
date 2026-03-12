"""Estimator abstract base class and registry."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class EstimatorState(ABC):
    """Base class for estimator-specific state."""

    @abstractmethod
    def to_dict(self) -> dict:
        ...

    @classmethod
    @abstractmethod
    def from_dict(cls, d: dict) -> "EstimatorState":
        ...


class Estimator(ABC):
    """Abstract estimator that tracks per-split performance."""

    name: str

    @abstractmethod
    def init_state(self, first_time: float, priors: dict) -> EstimatorState:
        """Initialize state from the first observed time."""
        ...

    @abstractmethod
    def process_attempt(
        self, state: EstimatorState, observed_time: float | None
    ) -> EstimatorState:
        """Process one attempt. observed_time=None for incomplete (death/abort)."""
        ...

    @abstractmethod
    def marginal_return(self, state: EstimatorState) -> float:
        """Compute marginal return m_i = -d_i / mu_i."""
        ...

    @abstractmethod
    def drift_info(self, state: EstimatorState) -> dict:
        """Return drift value, confidence interval, and label for dashboard."""
        ...

    @abstractmethod
    def get_population_priors(self, all_states: list[EstimatorState]) -> dict:
        """Compute population-level priors from all splits with enough data."""
        ...

    @abstractmethod
    def rebuild_state(self, attempts: list[float | None]) -> EstimatorState:
        """Rebuild state by replaying all attempts. None = incomplete."""
        ...


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
