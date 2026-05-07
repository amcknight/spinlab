"""Adapter: TransitionEvent -> JSON-dict shape consumed by protocol.parse_event.

Phase C emits typed dataclasses (events.py); session_manager already consumes
the JSON-dict shape that the Lua TCP path produces (protocol.py). This adapter
is the bridge -- single-direction, pure functions, used by RetroArchOrchestrator.

Field-mapping decisions:
- LevelEntrance.room/frame: dropped (the protocol shape doesn't have them).
- Death: collapsed to {"event": "death"} since DeathEvent has no payload.
- LevelExit: drops room/elapsed_ms/frame.
- Checkpoint: drops cp_type.
- Spawn: drops segment_id (consumed orchestrator-side as the StateIO key).
- state_path: empty string converted to None to match protocol's str | None type.
"""
from __future__ import annotations

from spinlab.retroarch.events import (
    Checkpoint,
    Death,
    LevelEntrance,
    LevelExit,
    Spawn,
    TransitionEvent,
)
from spinlab.retroarch.responses import StatusInfo


def _state_path_or_none(s: str) -> str | None:
    """Phase C uses '' for unset state_path; protocol uses None."""
    return s or None


def to_protocol_dict(event: TransitionEvent) -> dict:
    """Convert a Phase C TransitionEvent to the JSON-dict shape parse_event consumes."""
    if isinstance(event, LevelEntrance):
        return {
            "event": "level_entrance",
            "level": event.level,
            "state_path": _state_path_or_none(event.state_path),
            "timestamp_ms": event.timestamp_ms,
            "conditions": dict(event.conditions),
        }
    if isinstance(event, Death):
        return {"event": "death"}
    if isinstance(event, LevelExit):
        return {
            "event": "level_exit",
            "level": event.level,
            "goal": event.goal,
            "timestamp_ms": event.timestamp_ms,
            "conditions": dict(event.conditions),
        }
    if isinstance(event, Checkpoint):
        return {
            "event": "checkpoint",
            "level_num": event.level_num,
            "cp_ordinal": event.cp_ordinal,
            "state_path": _state_path_or_none(event.state_path),
            "timestamp_ms": event.timestamp_ms,
            "conditions": dict(event.conditions),
        }
    if isinstance(event, Spawn):
        return {
            "event": "spawn",
            "level_num": event.level_num,
            "state_captured": event.state_captured,
            "state_path": _state_path_or_none(event.state_path),
            "is_cold_cp": event.is_cold_cp,
            "cp_ordinal": event.cp_ordinal,
            "timestamp_ms": event.timestamp_ms,
            "conditions": dict(event.conditions),
        }
    raise TypeError(f"unsupported TransitionEvent subclass: {type(event).__name__}")


def to_rom_info_dict(status: StatusInfo) -> dict:
    """Build a `rom_info` JSON dict from NCI's GET_STATUS reply.

    Used at orchestrator startup to mimic the Lua-emitted RomInfoEvent.
    """
    return {
        "event": "rom_info",
        "filename": status.game or "",
    }
