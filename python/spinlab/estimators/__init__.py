"""Estimator abstract base class and registry."""
from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self, TypeVar

from spinlab import log

if TYPE_CHECKING:
    from spinlab.db import Database
    from spinlab.models import AttemptRecord, ModelOutput

logger = logging.getLogger(__name__)

S = TypeVar("S", bound="EstimatorState")


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
    """Base class for estimator-specific state.

    Concrete subclasses must declare their own fields after the inherited
    ``n_completed`` / ``n_attempts`` counters; the scheduler reads these two
    fields generically (e.g. to detect a bare state from a death-first
    attempt) so every estimator must keep them honest.
    """
    n_completed: int = 0
    n_attempts: int = 0

    _state_classes: ClassVar[dict[str, type["EstimatorState"]]] = {}

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
    def from_dict(cls, d: dict) -> Self:
        ...


def load_mature_states(
    db: "Database", game_id: str, estimator_name: str,
    state_cls: type[S], maturity_threshold: int,
) -> list[S]:
    """Load this estimator's saved states for a game and return mature ones.

    Mature = at least ``maturity_threshold`` completions, i.e. enough data to
    contribute meaningfully to population priors.  Used by Kalman and Exp Decay
    to compute their priors; both used to roll their own copy of this loop.
    """
    rows = db.load_all_model_states(game_id)
    states: list[S] = []
    for r in rows:
        if r["estimator"] != estimator_name or not r["state_json"]:
            continue
        try:
            states.append(state_cls.from_dict(json.loads(r["state_json"])))
        except (json.JSONDecodeError, KeyError) as exc:
            log.warn(
                logger, "skipped corrupt estimator state",
                exc=exc,
                segment_id=r.get("segment_id"),
                estimator=estimator_name,
                game_id=game_id,
            )
            continue
    return [s for s in states if s.n_completed >= maturity_threshold]


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
        self, state: EstimatorState, all_attempts: list["AttemptRecord"],
        params: dict | None = None,
    ) -> "ModelOutput":
        """Produce standardized ModelOutput from current state.

        ``params`` carries tunable estimator parameters (see ``declared_params``).
        Estimators that don't read params at output time can ignore it.
        """
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


def _register_all():
    """Import all estimator modules to trigger @register_estimator decorators."""
    from . import kalman, rolling_mean
    try:
        from . import exp_decay
    except ImportError:
        pass

_register_all()
