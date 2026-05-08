"""Segment-id naming for save state files. Single source of truth.

Both the RA-backend `StateIO.segment_id_for_event` and the capture
controllers' save-on-event hooks go through this helper so the naming
scheme has exactly one writer.
"""
from __future__ import annotations

from typing import Any

from spinlab.protocol import (
    CheckpointEvent,
    LevelEntranceEvent,
    SpawnEvent,
)


def segment_id_for_event(event: Any) -> str | None:
    """Return the segment-id key for a save-eligible event, or None.

    Naming conventions (match lua/spinlab.lua's filename layout):
      - LevelEntranceEvent  -> "entrance_<level>_<room>"
      - CheckpointEvent     -> "cp_<level>_<ordinal>_hot"
      - SpawnEvent          -> the event's segment_id (cold-fill captures)

    Returns ``None`` for Death, LevelExit, or Spawn events with no
    segment_id — those have no file to save.
    """
    if isinstance(event, LevelEntranceEvent):
        return f"entrance_{event.level}_{event.room}"
    if isinstance(event, CheckpointEvent):
        return f"cp_{event.level_num}_{event.cp_ordinal}_hot"
    if isinstance(event, SpawnEvent) and event.segment_id:
        return event.segment_id
    return None
