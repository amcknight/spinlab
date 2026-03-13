"""SpinLab data models."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


class TransitionEvent(str):
    LEVEL_START = "level_start"
    ROOM_CHANGE = "room_change"
    DEATH = "death"
    GOAL = "goal"
    CHECKPOINT = "checkpoint"


@dataclass
class Split:
    id: str
    game_id: str
    level_number: int
    room_id: Optional[int]
    goal: str
    description: str = ""
    state_path: Optional[str] = None
    reference_time_ms: Optional[int] = None
    strat_version: int = 1
    active: bool = True
    ordinal: Optional[int] = None
    reference_id: Optional[str] = None

    @staticmethod
    def make_id(game_id: str, level: int, room: int, goal: str) -> str:
        return f"{game_id}:{level}:{room}:{goal}"


@dataclass
class Attempt:
    split_id: str
    session_id: str
    completed: bool
    time_ms: int | None = None
    goal_matched: bool | None = None
    rating: str | None = None
    strat_version: int = 1
    source: str = "practice"
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class SplitCommand:
    """Sent from orchestrator to Lua: which split to load next."""
    id: str
    state_path: str
    goal: str
    description: str
    reference_time_ms: int | None
    auto_advance_delay_ms: int = 2000
    expected_time_ms: int | None = None  # Kalman μ*1000, falls back to reference_time_ms

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "state_path": self.state_path,
            "goal": self.goal,
            "description": self.description,
            "reference_time_ms": self.reference_time_ms,
            "auto_advance_delay_ms": self.auto_advance_delay_ms,
            "expected_time_ms": self.expected_time_ms,
        }
