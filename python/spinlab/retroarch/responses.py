"""Parsed response dataclasses for NCI commands."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StatusInfo:
    """Parsed reply from GET_STATUS.

    Examples:
      "GET_STATUS PLAYING super_nes,Toothpaste,crc32=41b3c49d"
        -> state=PLAYING, system=super_nes, game=Toothpaste, crc32=41b3c49d
      "GET_STATUS CONTENTLESS"
        -> state=CONTENTLESS, others None
    """

    state: str  # PLAYING | PAUSED | CONTENTLESS
    system: str | None = None
    game: str | None = None
    crc32: str | None = None
