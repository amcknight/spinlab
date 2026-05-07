"""StateIO — sync save/load + path resolution against RA + SpinLab filesystem.

Replaces lua/spinlab.lua's save_state_to_file/load_state_from_file plus the
pending_saves/pending_loads/cpuExec-deferred drain pattern. The cpuExec
deferral was a Mesen-specific requirement; NCI has no such constraint.

Phase D scope: this module owns the SAVE_STATE -> mtime-poll -> move flow,
and the reverse for load. Wiring into session_manager / practice.py / the
capture pipeline is Phase F-live.
"""
from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path

from spinlab.retroarch.events import (
    Checkpoint,
    LevelEntrance,
    Spawn,
    TransitionEvent,
)
from spinlab.retroarch.nci import NCIClient
from spinlab.retroarch.state_paths import (
    ra_slot_filename,
    segment_state_filename,
)

DEFAULT_RESERVED_SLOT = 9999  # see Decision 6 in Phase D plan
DEFAULT_SAVE_TIMEOUT_SEC = 1.0  # mtime-advance wait; healthy RA writes in <100ms

logger = logging.getLogger(__name__)


class StateSaveTimeout(RuntimeError):
    """SAVE_STATE was issued but the slot file mtime did not advance in time."""


class StateIO:
    """Sync owner of SpinLab's segment-keyed savestate files.

    Side-effecting; not thread-safe (don't share an instance across threads).
    Async callers wrap individual methods in `asyncio.to_thread` if they need
    to call from an event loop.
    """

    def __init__(
        self,
        client: NCIClient,
        ra_savestate_dir: Path,
        spinlab_state_dir: Path,
        ra_game_basename: str,
        reserved_slot: int = DEFAULT_RESERVED_SLOT,
        save_timeout_sec: float = DEFAULT_SAVE_TIMEOUT_SEC,
    ) -> None:
        self._client = client
        self._ra_dir = Path(ra_savestate_dir)
        self._sl_dir = Path(spinlab_state_dir)
        self._game_basename = ra_game_basename
        self._reserved_slot = reserved_slot
        self._save_timeout_sec = save_timeout_sec
        # Ensure SpinLab dir exists; the RA dir is RA's responsibility.
        self._sl_dir.mkdir(parents=True, exist_ok=True)

    # -- pure path resolution --------------------------------------------------

    def state_path_for(self, segment_id: str) -> Path:
        """Where SpinLab keeps the savestate for a given segment id."""
        return self._sl_dir / segment_state_filename(segment_id)

    def has_state_for(self, segment_id: str) -> bool:
        return self.state_path_for(segment_id).exists()

    def _ra_slot_path(self) -> Path:
        return self._ra_dir / ra_slot_filename(self._game_basename, self._reserved_slot)

    # -- event-shaped resolver -------------------------------------------------

    def resolve_event_path(self, event: TransitionEvent) -> str:
        """Resolver for `PollerDeps.state_path_for`.

        Returns the absolute path string to stamp onto the event, or "" when
        no path applies (Death, LevelExit, Spawn with no segment_id).

        Naming conventions chosen to match lua/spinlab.lua's filename layout
        but flattened (segment_id-keyed where possible):
        - LevelEntrance  -> "entrance_<level>_<room>"
        - Checkpoint     -> "cp_<level>_<ordinal>_hot"
        - Spawn(cold-fill) -> "<segment_id>"
        """
        if isinstance(event, LevelEntrance):
            return str(self.state_path_for(f"entrance_{event.level}_{event.room}"))
        if isinstance(event, Checkpoint):
            return str(self.state_path_for(f"cp_{event.level_num}_{event.cp_ordinal}_hot"))
        if isinstance(event, Spawn):
            if not event.segment_id:
                return ""
            return str(self.state_path_for(event.segment_id))
        return ""

    # -- save/load (Tasks 4 & 5; stubbed below until those tasks land) --------

    def save_segment_state(self, segment_id: str) -> Path:
        """Triggered by Task 4."""
        raise NotImplementedError("implemented in Task 4")

    def load_segment_state(self, segment_id: str) -> None:
        """Triggered by Task 5."""
        raise NotImplementedError("implemented in Task 5")
