"""SpinLab data models."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Optional


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
class ModelOutput:
    """What every estimator produces."""
    expected_time_ms: float             # E[total_time] for next attempt
    clean_expected_ms: float            # E[clean_tail] for next attempt
    ms_per_attempt: float               # improvement rate (positive = improving)
    floor_estimate_ms: float            # E[total_time | infinite practice]
    clean_floor_estimate_ms: float      # E[clean_tail | infinite practice]

    def to_dict(self) -> dict:
        return {
            "expected_time_ms": self.expected_time_ms,
            "clean_expected_ms": self.clean_expected_ms,
            "ms_per_attempt": self.ms_per_attempt,
            "floor_estimate_ms": self.floor_estimate_ms,
            "clean_floor_estimate_ms": self.clean_floor_estimate_ms,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ModelOutput":
        return cls(
            expected_time_ms=d["expected_time_ms"],
            clean_expected_ms=d["clean_expected_ms"],
            ms_per_attempt=d["ms_per_attempt"],
            floor_estimate_ms=d["floor_estimate_ms"],
            clean_floor_estimate_ms=d["clean_floor_estimate_ms"],
        )
