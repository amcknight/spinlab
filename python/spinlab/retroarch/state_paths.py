"""Pure path-helper functions for SpinLab + RA savestate filenames.

Kept separate from state_io so the path math is unit-testable without any
filesystem or NCI dependencies. state_io composes these to do its job.
"""
from __future__ import annotations

# Characters that segment_id may legitimately contain (game:level:cp idiom)
# but that we cannot put in a file name. Replaced with underscores.
_PATH_SEPARATOR_CHARS = (":", "/", "\\")

# Matches the file extension RA already uses for slot files
# (RA: <game>.state<N>; SpinLab: <segment>.state).
_SPINLAB_STATE_EXT = ".state"


def segment_state_filename(segment_id: str) -> str:
    """Filename for a SpinLab-managed savestate keyed by segment id.

    segment_ids in SpinLab can include colons (e.g. "game:5:cp1") and other
    separators that aren't filesystem-safe; replace them with underscores.
    """
    if not segment_id:
        raise ValueError("segment_id is empty")
    sanitized = segment_id
    for ch in _PATH_SEPARATOR_CHARS:
        sanitized = sanitized.replace(ch, "_")
    return sanitized + _SPINLAB_STATE_EXT


def ra_slot_filename(game_basename: str, slot: int) -> str:
    """Filename RA writes for a given slot.

    Mirrors the convention RA uses for save-state files in `savestate_directory`.
    Example: for game "Toothpaste World" and slot 9999, the file is
    "Toothpaste World.state9999". Slot 0 still gets the suffix ".state0".
    """
    return f"{game_basename}.state{slot}"
