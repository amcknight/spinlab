"""Transition events emitted by the polling loop.

Each event mirrors a JSON shape from lua/spinlab.lua's send_event calls.
session_manager and the dashboard already consume those JSON dicts; the
adapter that converts these dataclasses to the existing dict shape lives
in Phase F. For Phase C, dataclasses are the produced type.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TransitionEvent:
    """Marker base — every concrete event inherits this for typing."""

    timestamp_ms: int
    conditions: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class LevelEntrance(TransitionEvent):
    level: int = 0
    room: int = 0
    frame: int = 0
    state_path: str = ""


@dataclass(frozen=True)
class Death(TransitionEvent):
    level_num: int = 0


@dataclass(frozen=True)
class LevelExit(TransitionEvent):
    level: int = 0
    room: int = 0
    goal: str = ""
    elapsed_ms: int = 0
    frame: int = 0


@dataclass(frozen=True)
class Checkpoint(TransitionEvent):
    level_num: int = 0
    cp_type: str = ""
    cp_ordinal: int = 0
    state_path: str = ""


@dataclass(frozen=True)
class Spawn(TransitionEvent):
    level_num: int = 0
    is_cold_cp: bool = False
    cp_ordinal: int = 0
    state_captured: bool = False
    state_path: str = ""
    segment_id: str = ""
