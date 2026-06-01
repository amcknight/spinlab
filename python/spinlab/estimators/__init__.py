"""Estimator abstract base class — the model-ingestion seam.

Only one concrete estimator survives the model purge (Plan 2): the EMA-suite
sampler in ``em_suite_sampler``. The ABC and the ``EstimatorState`` dataclass
are kept as the seam Spec #3 plugs the next-generation PGM model into.

The scheduler and ``/api/model`` routes instantiate ``EmSuiteSamplerEstimator``
directly — there is no longer a name-keyed registry. State is reconstructed
from the per-segment event log on every call, so neither priors nor an
``init_state`` step is needed by the surviving model; both were removed when
the all-estimators loop collapsed (Plan 2 Task 4).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    from spinlab.models import AttemptRecord, EventAttempt, ModelOutput


@dataclass
class EstimatorState(ABC):
    """Base class for estimator-specific state.

    Concrete subclasses must declare their own fields after the inherited
    ``n_completed`` / ``n_attempts`` counters; downstream consumers (e.g.
    ``SegmentWithModel`` for the segments table) read these two fields
    generically, so every estimator must keep them honest.
    """
    n_completed: int = 0
    n_attempts: int = 0

    @abstractmethod
    def to_dict(self) -> dict:
        ...

    @classmethod
    @abstractmethod
    def from_dict(cls, d: dict) -> Self:
        ...


class Estimator(ABC):
    """Abstract estimator that tracks per-split performance.

    The surface is intentionally small: ``rebuild_state`` replays events into a
    state, ``model_output`` projects a state into the standardized
    ``ModelOutput`` shape. The scheduler calls both; the ``/api/model`` route
    and matrix route call both. Anything else is out of scope for this seam.
    """

    name: str
    display_name: str = ""

    @abstractmethod
    def model_output(
        self, state: "EstimatorState", all_attempts: list["AttemptRecord"],
        params: dict | None = None,
        events: list["EventAttempt"] | None = None,
    ) -> "ModelOutput":
        """Project state into the standardized ModelOutput shape.

        ``params`` is reserved for future tunable knobs (currently unused).
        ``events`` is the per-segment event list (one row per died/survived
        event); estimators that don't consume event-level data may ignore it.
        """
        ...

    @abstractmethod
    def rebuild_state(
        self, attempts: list["AttemptRecord"],
        params: dict | None = None,
        events: list["EventAttempt"] | None = None,
    ) -> "EstimatorState":
        """Rebuild state by replaying all attempts (and events, if used)."""
        ...
