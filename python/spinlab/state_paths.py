"""SpinLab segment-keyed savestate paths.

Pure data-model: maps a segment id (and the event types that produce one) to a
filename and on-disk location under SpinLab's per-game state directory. No
filesystem side-effects beyond the existence check in ``StatePathResolver.has``.

The RA backend talks to this module to know where to put / find segment files;
SpinLab capture controllers also use it directly when they need to reason
about a segment's location without going through the backend.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from spinlab.protocol import (
    CheckpointEvent,
    LevelEntranceEvent,
    SpawnEvent,
)

# segment_ids can include separators like ':' or '/' (e.g. "game:5:cp1"); those
# aren't filesystem-safe. Replace with underscores when forming a filename.
_PATH_SEPARATOR_CHARS = (":", "/", "\\")

_SPINLAB_STATE_EXT = ".state"


def segment_state_filename(segment_id: str) -> str:
    """Filename for a SpinLab-managed savestate keyed by segment id."""
    if not segment_id:
        raise ValueError("segment_id is empty")
    sanitized = segment_id
    for ch in _PATH_SEPARATOR_CHARS:
        sanitized = sanitized.replace(ch, "_")
    return sanitized + _SPINLAB_STATE_EXT


def segment_id_for_event(event: Any) -> str | None:
    """Return the segment id for a save-eligible event, or None.

    Naming conventions:
      - LevelEntranceEvent → ``entrance_<level>_<room>``
      - CheckpointEvent    → ``cp_<level>_<ordinal>_hot``
      - SpawnEvent         → the event's segment_id (cold-fill captures)

    Returns ``None`` for events that have no associated save (Death, LevelExit,
    Spawn without segment_id).
    """
    if isinstance(event, LevelEntranceEvent):
        return f"entrance_{event.level}_{event.room}"
    if isinstance(event, CheckpointEvent):
        return f"cp_{event.level_num}_{event.cp_ordinal}_hot"
    if isinstance(event, SpawnEvent) and event.segment_id:
        return event.segment_id
    return None


class StatePathResolver:
    """Bundles a SpinLab state directory with the segment-id helpers.

    Construct one per game session — the directory must already be scoped to
    the current game (e.g. ``<data_dir>/states/<game_id>/``) because segment
    ids do not include game_id and would collide across games otherwise.
    """

    def __init__(self, state_dir: Path) -> None:
        self._state_dir = Path(state_dir)
        self._state_dir.mkdir(parents=True, exist_ok=True)

    @property
    def state_dir(self) -> Path:
        return self._state_dir

    def path_for(self, segment_id: str) -> Path:
        """Absolute path where SpinLab keeps the savestate for this segment."""
        return self._state_dir / segment_state_filename(segment_id)

    def has(self, segment_id: str) -> bool:
        return self.path_for(segment_id).exists()

    def resolve_event(self, event: Any) -> str | None:
        """Resolver suitable for ``PollerDeps.state_path_for``.

        Returns the absolute path string for the segment this event would
        produce, or ``None`` if the event has no associated save.
        """
        seg_id = segment_id_for_event(event)
        return str(self.path_for(seg_id)) if seg_id else None
