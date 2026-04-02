"""SpinLab data models."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Optional


class Mode(Enum):
    IDLE = "idle"
    REFERENCE = "reference"
    PRACTICE = "practice"
    REPLAY = "replay"
    FILL_GAP = "fill_gap"


_LEGAL_TRANSITIONS: dict[Mode, set[Mode]] = {
    Mode.IDLE: {Mode.REFERENCE, Mode.PRACTICE, Mode.FILL_GAP},
    Mode.REFERENCE: {Mode.IDLE, Mode.REPLAY},
    Mode.PRACTICE: {Mode.IDLE},
    Mode.REPLAY: {Mode.IDLE},
    Mode.FILL_GAP: {Mode.IDLE},
}


def transition_mode(current: Mode, target: Mode) -> Mode:
    """Validate and return the target mode, or raise ValueError."""
    if target not in _LEGAL_TRANSITIONS.get(current, set()):
        raise ValueError(f"Illegal mode transition: {current.value} -> {target.value}")
    return target


class TransitionEvent(str):
    LEVEL_START = "level_start"
    ROOM_CHANGE = "room_change"
    DEATH = "death"
    GOAL = "goal"
    CHECKPOINT = "checkpoint"
    SPAWN = "spawn"


@dataclass
class Segment:
    id: str
    game_id: str
    level_number: int
    start_type: str          # 'entrance', 'checkpoint'
    start_ordinal: int
    end_type: str            # 'checkpoint', 'goal'
    end_ordinal: int
    description: str = ""
    strat_version: int = 1
    active: bool = True
    ordinal: Optional[int] = None
    reference_id: Optional[str] = None

    @staticmethod
    def make_id(game_id: str, level: int, start_type: str, start_ord: int,
                end_type: str, end_ord: int) -> str:
        return f"{game_id}:{level}:{start_type}.{start_ord}:{end_type}.{end_ord}"


@dataclass
class SegmentVariant:
    segment_id: str
    variant_type: str        # 'cold', 'hot'
    state_path: str
    is_default: bool = False


@dataclass
class Attempt:
    segment_id: str
    session_id: str
    completed: bool
    time_ms: int | None = None
    goal_matched: bool | None = None
    rating: str | None = None
    strat_version: int = 1
    source: str = "practice"
    deaths: int = 0
    clean_tail_ms: int | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


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
        return {
            "id": self.id,
            "state_path": self.state_path,
            "description": self.description,
            "end_type": self.end_type,
            "expected_time_ms": self.expected_time_ms,
            "auto_advance_delay_ms": self.auto_advance_delay_ms,
        }


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
        # V2 nested format
        if "total" in d:
            return cls(
                total=Estimate.from_dict(d["total"]),
                clean=Estimate.from_dict(d["clean"]),
            )
        # V1 backward compatibility: flat keys -> map to sides
        return cls(
            total=Estimate(
                expected_ms=d.get("expected_time_ms"),
                ms_per_attempt=d.get("ms_per_attempt"),
                floor_ms=d.get("floor_estimate_ms"),
            ),
            clean=Estimate(
                expected_ms=d.get("clean_expected_ms"),
                ms_per_attempt=None,
                floor_ms=d.get("clean_floor_estimate_ms"),
            ),
        )
