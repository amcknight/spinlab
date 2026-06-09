"""Typed event/command catalog for the dashboard ↔ emulator backend.

Events flow from the backend (orchestrator + poller) into SessionManager.
Commands flow from SessionManager into the backend. Dispatch on both sides
is by dataclass type — there is no JSON wire format at this seam.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, TypedDict

SPEED_UNCAPPED = 0  # passed to ReplayCmd.speed; RA interprets 0 as uncapped
SPEED_NORMAL = 1  # 1x realtime. MovieController only fast-forwards when
# speed == SPEED_UNCAPPED, so any non-zero value plays at RA's normal rate;
# 1 names "watch it play in real time" (the default for a user-clicked Replay).

# Values produced by retroarch/predicates.py::check_checkpoint_hit().
# Stored on CheckpointEvent.cp_type and persisted in the DB ``end_type``
# column for checkpoint segments (after recorder normalizes to "checkpoint").
CheckpointType = Literal["midway", "cp_entrance"]

# Values produced by retroarch/predicates.py::goal_type(). The recorder
# treats anything that isn't "abort" as a successful completion; ordering
# (key > orb > boss > normal > abort) is set by predicates.
LevelExitGoal = Literal["key", "orb", "boss", "normal", "abort"]


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
    conditions: dict[str, int] = field(default_factory=dict)

@dataclass(frozen=True)
class CheckpointEvent:
    level_num: int = 0
    cp_ordinal: int = 1
    cp_type: CheckpointType = "midway"
    state_path: str | None = None
    timestamp_ms: int = 0
    conditions: dict[str, int] = field(default_factory=dict)

@dataclass(frozen=True)
class DeathEvent:
    level_num: int = 0
    timestamp_ms: int = 0
    conditions: dict[str, int] = field(default_factory=dict)

@dataclass(frozen=True)
class SpawnEvent:
    level_num: int = 0
    state_path: str | None = None
    conditions: dict[str, int] = field(default_factory=dict)
    is_cold_cp: bool = False
    cp_ordinal: int | None = None
    segment_id: str = ""
    timestamp_ms: int = 0

@dataclass(frozen=True)
class LevelExitEvent:
    level: int = 0
    room: int = 0
    goal: LevelExitGoal = "abort"
    elapsed_ms: int = 0
    frame: int = 0
    timestamp_ms: int = 0
    conditions: dict[str, int] = field(default_factory=dict)

@dataclass(frozen=True)
class AttemptResultEvent:
    segment_id: str = ""
    completed: bool = False
    time_ms: int | None = None
    deaths: int = 0
    clean_tail_ms: int | None = None


# Emitted by PracticeTiming / HyperPlayTiming once per died/survived event
# during an armed attempt. The orchestrator (PracticeSession,
# HyperPlaySession) is responsible for stamping in session_id / source /
# chosen_allocator and writing the storage-shaped EventAttempt to the DB.
# See models.EventAttempt for the storage record.
@dataclass(frozen=True)
class EventAttemptEmission:
    segment_id: str = ""
    episode_id: str = ""
    outcome: Literal["died", "survived"] = "died"
    time_ms: int = 0
    timestamp_ms: int = 0

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
class ControllerCommandEvent:
    """An R-menu command dispatched from the controller. command is a key from
    the menu_detector COMMANDS registry (today only "pause")."""
    command: str = ""

@dataclass(frozen=True)
class ControllerMenuArmedEvent:
    """The R-menu arm state changed (R held past threshold / R released).
    Drives the dashboard's 'X — Pause' hint."""
    armed: bool = False

@dataclass(frozen=True)
class HyperPlayCheckpointEvent:
    ordinal: int = 0
    elapsed_ms: int = 0
    split_ms: int = 0

@dataclass(frozen=True)
class HyperPlayDeathEvent:
    elapsed_ms: int = 0
    split_ms: int = 0

@dataclass(frozen=True)
class HyperPlayCompleteEvent:
    elapsed_ms: int = 0
    split_ms: int = 0


# ---------------------------------------------------------------------------
# Event unions
#
# PollerEvent: events the Poller can emit (memory-driven transitions + the
# infrastructure events stamped onto the same stream by the orchestrator).
# MovieEvent: events the MovieController emits during replay playback.
# Used as the parameter type of callback signatures so call sites stop
# defaulting to `Any`.
# ---------------------------------------------------------------------------

PollerEvent = (
    RomInfoEvent
    | GameContextEvent
    | LevelEntranceEvent
    | CheckpointEvent
    | DeathEvent
    | SpawnEvent
    | LevelExitEvent
)

MovieEvent = ReplayStartedEvent | ReplayFinishedEvent | ReplayErrorEvent


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

@dataclass(frozen=True)
class ConditionSpec:
    """A condition definition sent from SessionManager to the backend.

    The backend's poller uses this to issue NCI READ_CORE_RAM requests when
    stamping events with current memory-condition values. Only name + address
    + size are needed at read time; the type/values/scope used for capture-side
    decoding stay with the YAML-loaded ConditionDef in condition_registry.py.
    """
    name: str
    address: int
    size: int  # bytes; only 1 or 2 are supported by read_all


@dataclass
class SetConditionsCmd:
    definitions: list[ConditionSpec] = field(default_factory=list)

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
class PracticePauseCmd:
    """Disarm the backend PracticeTiming without ending the practice loop.
    Sent by PracticeSession.toggle_pause to drop the in-flight attempt; resume
    re-arms by re-sending the stashed PracticeLoadCmd."""
    pass

class HyperPlayCheckpoint(TypedDict):
    ordinal: int
    state_path: str


@dataclass
class HyperPlayLoadCmd:
    id: str = ""
    state_path: str = ""
    description: str = ""
    checkpoints: list[HyperPlayCheckpoint] = field(default_factory=list)
    expected_time_ms: int | None = None
    auto_advance_delay_ms: int = 1000
    # Length of the post-death blackout before the cold save state is reloaded.
    # See HyperPlaySession.death_delay_ms for rationale.
    death_delay_ms: int = 0

@dataclass
class HyperPlayStopCmd:
    pass

@dataclass
class ResetCmd:
    """Power-cycle the emulator (back to title screen)."""
    pass
