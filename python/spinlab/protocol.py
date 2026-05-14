"""Typed event/command catalog for the dashboard ↔ emulator backend.

Events flow from the backend (orchestrator + poller) into SessionManager.
Commands flow from SessionManager into the backend. Dispatch on both sides
is by dataclass type — there is no JSON wire format at this seam.
"""
from __future__ import annotations

from dataclasses import dataclass, field

SPEED_UNCAPPED = 0  # passed to ReplayCmd.speed; RA interprets 0 as uncapped


# ---------------------------------------------------------------------------
# Events (backend → SessionManager)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RomInfoEvent:
    filename: str = ""

@dataclass(frozen=True)
class GameContextEvent:
    game_id: str = ""
    game_name: str = ""

@dataclass(frozen=True)
class LevelEntranceEvent:
    level: int = 0
    room: int = 0
    frame: int = 0
    state_path: str | None = None
    timestamp_ms: int = 0
    conditions: dict = field(default_factory=dict)

@dataclass(frozen=True)
class CheckpointEvent:
    level_num: int = 0
    cp_ordinal: int = 1
    cp_type: str = ""
    state_path: str | None = None
    timestamp_ms: int = 0
    conditions: dict = field(default_factory=dict)

@dataclass(frozen=True)
class DeathEvent:
    level_num: int = 0
    timestamp_ms: int = 0
    conditions: dict = field(default_factory=dict)

@dataclass(frozen=True)
class SpawnEvent:
    level_num: int = 0
    state_path: str | None = None
    conditions: dict = field(default_factory=dict)
    is_cold_cp: bool = False
    cp_ordinal: int | None = None
    segment_id: str = ""
    timestamp_ms: int = 0

@dataclass(frozen=True)
class LevelExitEvent:
    level: int = 0
    room: int = 0
    goal: str = "abort"
    elapsed_ms: int = 0
    frame: int = 0
    timestamp_ms: int = 0
    conditions: dict = field(default_factory=dict)

@dataclass(frozen=True)
class AttemptResultEvent:
    segment_id: str = ""
    completed: bool = False
    time_ms: int | None = None
    deaths: int = 0
    clean_tail_ms: int | None = None

@dataclass(frozen=True)
class ReplayStartedEvent:
    path: str = ""
    frame_count: int = 0

@dataclass(frozen=True)
class ReplayFinishedEvent:
    pass

@dataclass(frozen=True)
class ReplayErrorEvent:
    message: str = ""

@dataclass(frozen=True)
class AttemptInvalidatedEvent:
    pass

@dataclass(frozen=True)
class SpeedRunCheckpointEvent:
    ordinal: int = 0
    elapsed_ms: int = 0
    split_ms: int = 0

@dataclass(frozen=True)
class SpeedRunDeathEvent:
    elapsed_ms: int = 0
    split_ms: int = 0

@dataclass(frozen=True)
class SpeedRunCompleteEvent:
    elapsed_ms: int = 0
    split_ms: int = 0


# ---------------------------------------------------------------------------
# Commands (SessionManager → backend)
# ---------------------------------------------------------------------------

@dataclass
class ReferenceStartCmd:
    path: str = ""

@dataclass
class ReferenceStopCmd:
    pass

@dataclass
class ReplayCmd:
    path: str = ""
    speed: int = SPEED_UNCAPPED

@dataclass
class ReplayStopCmd:
    pass

@dataclass
class FillGapLoadCmd:
    state_path: str = ""
    message: str = ""

@dataclass
class ColdFillLoadCmd:
    state_path: str = ""
    segment_id: str = ""

@dataclass
class SetConditionsCmd:
    definitions: list[dict] = field(default_factory=list)

@dataclass
class PracticeLoadCmd:
    id: str = ""
    state_path: str = ""
    description: str = ""
    end_type: str = ""
    expected_time_ms: int | None = None
    auto_advance_delay_ms: int = 1000
    death_penalty_ms: int = 3200

@dataclass
class PracticeStopCmd:
    pass

@dataclass
class SpeedRunLoadCmd:
    id: str = ""
    state_path: str = ""
    description: str = ""
    checkpoints: list = field(default_factory=list)
    expected_time_ms: int | None = None
    auto_advance_delay_ms: int = 1000
    # Length of the post-death blackout before the cold save state is reloaded.
    # See SpeedRunSession.death_delay_ms for rationale.
    death_delay_ms: int = 1500

@dataclass
class SpeedRunStopCmd:
    pass

@dataclass
class ResetCmd:
    """Power-cycle the emulator (back to title screen)."""
    pass
