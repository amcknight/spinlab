"""SpinLab data models."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class Rating(str, Enum):
    AGAIN = "again"
    HARD = "hard"
    GOOD = "good"
    EASY = "easy"
    SKIP = "skip"

    @property
    def sm2_quality(self) -> int:
        """Map to SM-2 quality score (0-5)."""
        return {
            Rating.AGAIN: 1,
            Rating.HARD: 3,
            Rating.GOOD: 4,
            Rating.EASY: 5,
            Rating.SKIP: -1,  # skip doesn't update schedule
        }[self]


class TransitionEvent(str, Enum):
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

    @staticmethod
    def make_id(game_id: str, level: int, room: int, goal: str) -> str:
        return f"{game_id}:{level}:{room}:{goal}"


@dataclass
class Schedule:
    split_id: str
    ease_factor: float = 2.5
    interval_minutes: float = 5.0
    repetitions: int = 0
    next_review: datetime = field(default_factory=datetime.utcnow)

    SM2_MIN_EF: float = 1.3

    def update(self, rating: Rating, base_interval: float = 5.0) -> None:
        """Apply SM-2 update given a rating."""
        if rating == Rating.SKIP:
            return

        q = rating.sm2_quality

        if q < 3:
            # Failed — reset
            self.repetitions = 0
            self.interval_minutes = base_interval
        else:
            # Successful
            if self.repetitions == 0:
                self.interval_minutes = base_interval
            elif self.repetitions == 1:
                self.interval_minutes = base_interval * 6
            else:
                self.interval_minutes = self.interval_minutes * self.ease_factor

            self.repetitions += 1

        # Update ease factor
        self.ease_factor = self.ease_factor + (0.1 - (5 - q) * (0.08 + (5 - q) * 0.02))
        self.ease_factor = max(self.ease_factor, self.SM2_MIN_EF)

        # Set next review time
        from datetime import timedelta
        self.next_review = datetime.utcnow() + timedelta(minutes=self.interval_minutes)


@dataclass
class Attempt:
    split_id: str
    session_id: str
    completed: bool
    time_ms: Optional[int] = None
    goal_matched: Optional[bool] = None
    rating: Optional[Rating] = None
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
    reference_time_ms: Optional[int]

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "state_path": self.state_path,
            "goal": self.goal,
            "description": self.description,
            "reference_time_ms": self.reference_time_ms,
        }
