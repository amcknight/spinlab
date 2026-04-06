"""SystemState — single source of truth for what the system is doing right now."""
from __future__ import annotations

from dataclasses import dataclass

from .models import Mode


@dataclass
class CaptureState:
    """Active reference or replay capture."""
    run_id: str
    rec_path: str | None = None
    segments_count: int = 0


@dataclass
class DraftState:
    """Pending draft after capture (waiting for save/discard)."""
    run_id: str
    segment_count: int


@dataclass
class ColdFillState:
    """Cold-fill queue progress."""
    current_segment_id: str
    current_num: int
    total: int
    segment_label: str


@dataclass
class FillGapState:
    """Fill-gap for a single segment."""
    segment_id: str
    waypoint_id: str


@dataclass
class PracticeState:
    """Active practice session."""
    session_id: str
    started_at: str
    current_segment_id: str | None = None
    segments_attempted: int = 0
    segments_completed: int = 0


@dataclass
class SystemState:
    """Single source of truth for the system's current mode and associated sub-state."""
    mode: Mode = Mode.IDLE
    game_id: str | None = None
    game_name: str | None = None
    capture: CaptureState | None = None
    draft: DraftState | None = None
    cold_fill: ColdFillState | None = None
    fill_gap: FillGapState | None = None
    practice: PracticeState | None = None
