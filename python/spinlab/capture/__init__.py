"""Capture pipeline: segment recording, draft lifecycle, reference/replay/cold-fill orchestration."""
from .draft import DraftManager
from .recorder import SegmentRecorder, RecordedSegmentTime
from .reference import ReferenceController

__all__ = ["DraftManager", "ReferenceController", "SegmentRecorder", "RecordedSegmentTime"]
