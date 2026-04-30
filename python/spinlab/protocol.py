# python/spinlab/protocol.py
"""Typed TCP protocol — message catalog for Lua <-> Python communication.

Every Lua->Python event and Python->Lua command is a dataclass here.
This file is the single source of truth for the IPC contract.
"""
from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field

# API uses speed=0 to mean "uncapped / as fast as possible".
# Mesen's emu.setSpeed(0) means "paused" — so the Lua side must
# never pass SPEED_UNCAPPED directly to setSpeed.
SPEED_UNCAPPED = 0

# ---------------------------------------------------------------------------
# Lua -> Python events
# ---------------------------------------------------------------------------

@dataclass
class RomInfoEvent:
    event: str = "rom_info"
    filename: str = ""

@dataclass
class GameContextEvent:
    event: str = "game_context"
    game_id: str = ""
    game_name: str = ""

@dataclass
class LevelEntranceEvent:
    event: str = "level_entrance"
    level: int = 0
    state_path: str | None = None
    timestamp_ms: int = 0
    conditions: dict = field(default_factory=dict)

@dataclass
class CheckpointEvent:
    event: str = "checkpoint"
    level_num: int = 0
    cp_ordinal: int = 1
    state_path: str | None = None
    timestamp_ms: int = 0
    conditions: dict = field(default_factory=dict)

@dataclass
class DeathEvent:
    event: str = "death"

@dataclass
class SpawnEvent:
    event: str = "spawn"
    level_num: int = 0
    state_captured: bool = False
    state_path: str | None = None
    conditions: dict = field(default_factory=dict)
    is_cold_cp: bool = False
    cp_ordinal: int | None = None

@dataclass
class LevelExitEvent:
    event: str = "level_exit"
    level: int = 0
    goal: str = "abort"
    timestamp_ms: int = 0
    conditions: dict = field(default_factory=dict)

@dataclass
class AttemptResultEvent:
    event: str = "attempt_result"
    segment_id: str = ""
    completed: bool = False
    time_ms: int | None = None
    deaths: int = 0
    clean_tail_ms: int | None = None

@dataclass
class RecSavedEvent:
    event: str = "rec_saved"
    path: str = ""
    frame_count: int = 0

@dataclass
class ReplayStartedEvent:
    event: str = "replay_started"
    path: str = ""
    frame_count: int = 0

@dataclass
class ReplayProgressEvent:
    event: str = "replay_progress"
    frame: int = 0
    total: int = 0

@dataclass
class ReplayFinishedEvent:
    event: str = "replay_finished"

@dataclass
class ReplayErrorEvent:
    event: str = "replay_error"
    message: str = ""

@dataclass
class AttemptInvalidatedEvent:
    event: str = "attempt_invalidated"

@dataclass
class SpeedRunCheckpointEvent:
    event: str = "speed_run_checkpoint"
    ordinal: int = 0
    elapsed_ms: int = 0
    split_ms: int = 0

@dataclass
class SpeedRunDeathEvent:
    event: str = "speed_run_death"
    elapsed_ms: int = 0
    split_ms: int = 0

@dataclass
class SpeedRunCompleteEvent:
    event: str = "speed_run_complete"
    elapsed_ms: int = 0
    split_ms: int = 0


# ---------------------------------------------------------------------------
# Python -> Lua commands
# ---------------------------------------------------------------------------

@dataclass
class GameContextCmd:
    event: str = "game_context"
    game_id: str = ""
    game_name: str = ""

@dataclass
class ReferenceStartCmd:
    event: str = "reference_start"
    path: str = ""

@dataclass
class ReferenceStopCmd:
    event: str = "reference_stop"

@dataclass
class ReplayCmd:
    event: str = "replay"
    path: str = ""
    speed: int = SPEED_UNCAPPED

@dataclass
class ReplayStopCmd:
    event: str = "replay_stop"

@dataclass
class FillGapLoadCmd:
    event: str = "fill_gap_load"
    state_path: str = ""
    message: str = ""

@dataclass
class ColdFillLoadCmd:
    event: str = "cold_fill_load"
    state_path: str = ""
    segment_id: str = ""

@dataclass
class SetConditionsCmd:
    event: str = "set_conditions"
    definitions: list[dict] = field(default_factory=list)

@dataclass
class SetInvalidateComboCmd:
    event: str = "set_invalidate_combo"
    combo: list[str] = field(default_factory=list)

@dataclass
class PracticeLoadCmd:
    event: str = "practice_load"
    id: str = ""
    state_path: str = ""
    description: str = ""
    end_type: str = ""
    expected_time_ms: int | None = None
    auto_advance_delay_ms: int = 1000
    death_penalty_ms: int = 3200

@dataclass
class PracticeStopCmd:
    event: str = "practice_stop"

@dataclass
class SpeedRunLoadCmd:
    event: str = "speed_run_load"
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
    event: str = "speed_run_stop"


# ---------------------------------------------------------------------------
# Event registry and parser
# ---------------------------------------------------------------------------

_EVENT_REGISTRY: dict[str, type] = {
    "rom_info": RomInfoEvent,
    "game_context": GameContextEvent,
    "level_entrance": LevelEntranceEvent,
    "checkpoint": CheckpointEvent,
    "death": DeathEvent,
    "spawn": SpawnEvent,
    "level_exit": LevelExitEvent,
    "attempt_result": AttemptResultEvent,
    "rec_saved": RecSavedEvent,
    "replay_started": ReplayStartedEvent,
    "replay_progress": ReplayProgressEvent,
    "replay_finished": ReplayFinishedEvent,
    "replay_error": ReplayErrorEvent,
    "attempt_invalidated": AttemptInvalidatedEvent,
    "speed_run_checkpoint": SpeedRunCheckpointEvent,
    "speed_run_death": SpeedRunDeathEvent,
    "speed_run_complete": SpeedRunCompleteEvent,
}


def parse_event(raw: dict) -> object:
    """Parse a raw JSON dict from Lua into a typed event dataclass.

    Raises ValueError for unknown or malformed events.
    """
    event_name = raw.get("event")
    if event_name is None:
        raise ValueError("Missing 'event' field in TCP message")
    cls = _EVENT_REGISTRY.get(event_name)
    if cls is None:
        raise ValueError(f"Unknown event type: {event_name!r}")
    valid_fields = {f.name for f in dataclasses.fields(cls)}
    kwargs = {k: v for k, v in raw.items() if k in valid_fields}
    return cls(**kwargs)


def serialize_command(cmd) -> str:
    """Serialize a command dataclass to JSON string for sending over TCP."""
    return json.dumps(dataclasses.asdict(cmd))
