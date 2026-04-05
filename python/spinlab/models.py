"""SpinLab data models."""

import dataclasses
import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum, StrEnum
from typing import Optional


class Mode(Enum):
    IDLE = "idle"
    REFERENCE = "reference"
    PRACTICE = "practice"
    REPLAY = "replay"
    FILL_GAP = "fill_gap"
    COLD_FILL = "cold_fill"


_LEGAL_TRANSITIONS: dict[Mode, set[Mode]] = {
    Mode.IDLE: {Mode.REFERENCE, Mode.PRACTICE, Mode.FILL_GAP, Mode.COLD_FILL},
    Mode.REFERENCE: {Mode.IDLE, Mode.REPLAY},
    Mode.PRACTICE: {Mode.IDLE},
    Mode.REPLAY: {Mode.IDLE},
    Mode.FILL_GAP: {Mode.IDLE},
    Mode.COLD_FILL: {Mode.IDLE},
}


def transition_mode(current: Mode, target: Mode) -> Mode:
    """Validate and return the target mode, or raise ValueError."""
    if target not in _LEGAL_TRANSITIONS.get(current, set()):
        raise ValueError(f"Illegal mode transition: {current.value} -> {target.value}")
    return target


class EndpointType(StrEnum):
    ENTRANCE = "entrance"
    CHECKPOINT = "checkpoint"
    GOAL = "goal"


class EventType(StrEnum):
    ROM_INFO = "rom_info"
    GAME_CONTEXT = "game_context"
    LEVEL_ENTRANCE = "level_entrance"
    CHECKPOINT = "checkpoint"
    DEATH = "death"
    SPAWN = "spawn"
    LEVEL_EXIT = "level_exit"
    ATTEMPT_RESULT = "attempt_result"
    REC_SAVED = "rec_saved"
    REPLAY_STARTED = "replay_started"
    REPLAY_PROGRESS = "replay_progress"
    REPLAY_FINISHED = "replay_finished"
    REPLAY_ERROR = "replay_error"


class Status(StrEnum):
    OK = "ok"
    STARTED = "started"
    STOPPED = "stopped"
    NOT_CONNECTED = "not_connected"
    DRAFT_PENDING = "draft_pending"
    PRACTICE_ACTIVE = "practice_active"
    REFERENCE_ACTIVE = "reference_active"
    ALREADY_RUNNING = "already_running"
    ALREADY_REPLAYING = "already_replaying"
    NOT_IN_REFERENCE = "not_in_reference"
    NOT_REPLAYING = "not_replaying"
    NOT_RUNNING = "not_running"
    NO_DRAFT = "no_draft"
    NO_HOT_VARIANT = "no_hot_variant"
    NO_GAPS = "no_gaps"
    SHUTTING_DOWN = "shutting_down"


class AttemptSource(StrEnum):
    PRACTICE = "practice"
    REPLAY = "replay"


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
    strat_version: int = 1
    active: bool = True
    ordinal: Optional[int] = None
    reference_id: Optional[str] = None
    start_waypoint_id: Optional[str] = None
    end_waypoint_id: Optional[str] = None
    is_primary: bool = True

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
    def make(game_id: str, level_number: int, endpoint_type: str,
             ordinal: int, conditions: dict) -> "Waypoint":
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
    is_default: bool = False


@dataclass
class Attempt:
    segment_id: str
    session_id: str
    completed: bool
    time_ms: int | None = None
    strat_version: int = 1
    source: AttemptSource = AttemptSource.PRACTICE
    deaths: int = 0
    clean_tail_ms: int | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    observed_start_conditions: str | None = None
    observed_end_conditions: str | None = None
    invalidated: bool = False


@dataclass
class SegmentCommand:
    """Sent from orchestrator to Lua: which segment to load next."""
    id: str
    state_path: str
    description: str
    end_type: str              # 'checkpoint' or 'goal'
    expected_time_ms: int | None = None
    auto_advance_delay_ms: int = 1000

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


@dataclass
class AttemptRecord:
    """Attempt data flowing through the estimator pipeline."""
    time_ms: int | None          # total time including deaths; None if incomplete
    completed: bool
    deaths: int                  # 0 if clean
    clean_tail_ms: int | None    # time from last death to finish; None if incomplete
    created_at: str              # ISO timestamp


@dataclass
class Estimate:
    """One coherent set of predictions for a single time series."""
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


@dataclass
class ModelOutput:
    """What every estimator produces — predictions for total time and clean tail."""
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
