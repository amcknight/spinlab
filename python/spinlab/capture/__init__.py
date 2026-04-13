"""Capture pipeline: segment recording, draft lifecycle, reference/replay/cold-fill orchestration."""
from .recorder import SegmentRecorder, RecordedSegmentTime

__all__ = ["SegmentRecorder", "RecordedSegmentTime"]
