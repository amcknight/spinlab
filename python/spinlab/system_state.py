"""SystemState — single source of truth for what the system is doing right now."""
from __future__ import annotations

from dataclasses import dataclass

from .models import Mode


@dataclass
class SystemState:
    """Single source of truth for the system's current mode and associated sub-state."""
    mode: Mode = Mode.IDLE
    game_id: str | None = None
    game_name: str | None = None
    # R-menu armed (R held past threshold). Drives the 'X — Pause' hint; pushed
    # to the route bar via the live-summary payload. Mode-agnostic at the
    # detector; the frontend only renders it during practice.
    menu_armed: bool = False
