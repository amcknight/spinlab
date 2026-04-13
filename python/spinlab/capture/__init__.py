"""Capture pipeline: segment recording, draft lifecycle, reference/replay/cold-fill orchestration."""
from .cold_fill import ColdFillController
from .draft import DraftManager
from .recorder import SegmentRecorder, RecordedSegmentTime
from .reference import ReferenceController

__all__ = [
    "ColdFillController",
    "DraftManager",
    "ReferenceController",
    "SegmentRecorder",
    "RecordedSegmentTime",
]
