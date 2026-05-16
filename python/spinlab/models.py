"""SpinLab data models."""

import dataclasses
import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum, StrEnum
from typing import Optional

from pydantic import ConfigDict
from pydantic.dataclasses import dataclass as pydantic_dataclass


class Mode(Enum):
    IDLE = "idle"
    REFERENCE = "reference"
    PRACTICE = "practice"
    REPLAY = "replay"
    FILL_GAP = "fill_gap"
    COLD_FILL = "cold_fill"
    SPEED_RUN = "speed_run"


_LEGAL_TRANSITIONS: dict[Mode, set[Mode]] = {
    Mode.IDLE: {Mode.REFERENCE, Mode.PRACTICE, Mode.FILL_GAP, Mode.COLD_FILL, Mode.SPEED_RUN},
    Mode.REFERENCE: {Mode.IDLE, Mode.REPLAY},
    Mode.PRACTICE: {Mode.IDLE},
    Mode.REPLAY: {Mode.IDLE},
    Mode.FILL_GAP: {Mode.IDLE},
    Mode.COLD_FILL: {Mode.IDLE},
    Mode.SPEED_RUN: {Mode.IDLE},
}


def transition_mode(current: Mode, target: Mode) -> Mode:
    """Validate and return the target mode, or raise ValueError."""
    if target not in _LEGAL_TRANSITIONS.get(current, set()):
        raise ValueError(f"Illegal mode transition: {current.value} -> {target.value}")
    return target


# Decoded condition values: enum conditions as str labels, bool conditions as bool.
ConditionMap = dict[str, str | bool]


class EndpointType(StrEnum):
    ENTRANCE = "entrance"
    CHECKPOINT = "checkpoint"
    GOAL = "goal"


class Status(StrEnum):
    """Success outcomes from controller actions. Errors are raised as ActionError subclasses."""
    OK = "ok"
    STARTED = "started"
    STOPPED = "stopped"
    NO_GAPS = "no_gaps"


class AttemptSource(StrEnum):
    PRACTICE = "practice"
    REPLAY = "replay"
    REFERENCE = "reference"
    SPEED_RUN = "speed_run"


@dataclass
class ActionResult:
    """Typed result from controller actions. Replaces untyped status dicts."""
    status: Status
    new_mode: Mode | None = None
    session_id: str | None = None

    def to_response(self) -> dict:
        """API-safe dict — strips internal fields like new_mode."""
        d: dict = {"status": self.status.value}
        if self.session_id is not None:
            d["session_id"] = self.session_id
        return d


@dataclass
class Segment:
    id: str
    game_id: str
    level_number: int
    start_type: EndpointType
    start_ordinal: int
    end_type: EndpointType
    end_ordinal: int
    description: str = ""
    active: bool = True
    ordinal: int = 0
    capture_run_id: Optional[str] = None
    start_waypoint_id: Optional[str] = None
    end_waypoint_id: Optional[str] = None
    is_primary: bool = True
    capture_session_id: Optional[str] = None

    @staticmethod
    def make_id(game_id: str, level: int, start_type: str, start_ord: int,
                end_type: str, end_ord: int,
                start_waypoint_id: str, end_waypoint_id: str) -> str:
        return (f"{game_id}:{level}:{start_type}.{start_ord}:{end_type}.{end_ord}"
                f":{start_waypoint_id[:8]}:{end_waypoint_id[:8]}")


@dataclass
class Waypoint:
    id: str
    game_id: str
    level_number: int
    endpoint_type: EndpointType
    ordinal: int
    conditions_json: str     # canonical JSON (sorted keys)

    @staticmethod
    def make(game_id: str, level_number: int, endpoint_type: "EndpointType",
             ordinal: int, conditions: ConditionMap) -> "Waypoint":
        canonical = json.dumps(conditions, sort_keys=True, separators=(", ", ": "))
        h = hashlib.sha256(
            f"{game_id}:{level_number}:{endpoint_type}.{ordinal}:{canonical}".encode()
        ).hexdigest()[:16]
        return Waypoint(
            id=h,
            game_id=game_id,
            level_number=level_number,
            endpoint_type=endpoint_type,
            ordinal=ordinal,
            conditions_json=canonical,
        )


@dataclass
class WaypointSaveState:
    waypoint_id: str
    variant_type: str        # 'cold', 'hot'
    state_path: str


@dataclass
class Attempt:
    """An attempt of a segment.

    Exactly one of ``session_id`` or ``capture_run_id`` is set, enforced by a
    DB CHECK constraint. Practice and speed-run attempts use ``session_id``
    (FK to ``sessions``); reference-seeded attempts use ``capture_run_id``
    (FK to ``capture_runs``). ``source`` distinguishes practice from speed-run
    within the session-attempt category.
    """
    segment_id: str
    completed: bool
    session_id: str | None = None
    capture_run_id: str | None = None
    time_ms: int | None = None
    source: AttemptSource = AttemptSource.PRACTICE
    deaths: int = 0
    clean_tail_ms: int | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    invalidated: bool = False
    chosen_allocator: str | None = None

    def __post_init__(self) -> None:
        if (self.session_id is None) == (self.capture_run_id is None):
            raise ValueError(
                "Attempt requires exactly one of session_id or capture_run_id"
            )


@dataclass
class SegmentCommand:
    """Practice-loop directive: which segment to load next."""
    id: str
    state_path: str
    description: str
    end_type: str              # 'checkpoint' or 'goal'
    expected_time_ms: int | None = None
    auto_advance_delay_ms: int = 1000
    death_penalty_ms: int = 3200

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


class CaptureRunStatus(StrEnum):
    DRAFT = "draft"
    SAVED = "saved"


class CaptureRunKind(StrEnum):
    LIVE = "live"
    REPLAY = "replay"


@dataclass
class AttemptRecord:
    """Attempt data flowing through the estimator pipeline."""
    time_ms: int | None          # total time including deaths; None if incomplete
    completed: bool
    deaths: int                  # 0 if clean
    clean_tail_ms: int | None    # time from last death to finish; None if incomplete
    created_at: str              # ISO timestamp


@pydantic_dataclass(config=ConfigDict(extra="allow"))
class Estimate:
    """One coherent set of predictions for a single time series.

    Pydantic dataclass: behaves as a plain @dataclass at runtime (asdict,
    fields, ``to_dict``/``from_dict`` all work) AND emits an OpenAPI schema
    so api_schemas.py can re-export it rather than re-declaring.
    """
    expected_ms: float | None = None
    ms_per_attempt: float | None = None
    floor_ms: float | None = None

    def to_dict(self) -> dict:
        return {
            "expected_ms": self.expected_ms,
            "ms_per_attempt": self.ms_per_attempt,
            "floor_ms": self.floor_ms,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Estimate":
        return cls(
            expected_ms=d.get("expected_ms"),
            ms_per_attempt=d.get("ms_per_attempt"),
            floor_ms=d.get("floor_ms"),
        )


@pydantic_dataclass(config=ConfigDict(extra="allow"))
class ModelOutput:
    """What every estimator produces — predictions for total time and clean tail.

    Pydantic dataclass: see ``Estimate`` for rationale.
    """
    total: Estimate
    clean: Estimate

    def to_dict(self) -> dict:
        return {
            "total": self.total.to_dict(),
            "clean": self.clean.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ModelOutput":
        return cls(
            total=Estimate.from_dict(d["total"]),
            clean=Estimate.from_dict(d["clean"]),
        )
