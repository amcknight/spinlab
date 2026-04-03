"""Shared test data factories — consolidates duplicated helpers."""
from __future__ import annotations

from spinlab.models import AttemptRecord, Estimate, ModelOutput
from spinlab.allocators import SegmentWithModel


def make_attempt_record(
    time_ms: int,
    completed: bool,
    deaths: int = 0,
    clean_tail_ms: int | None = None,
    created_at: str = "2026-01-01T00:00:00",
) -> AttemptRecord:
    """Create an AttemptRecord for testing."""
    return AttemptRecord(
        time_ms=time_ms if completed else None,
        completed=completed,
        deaths=deaths,
        clean_tail_ms=clean_tail_ms,
        created_at=created_at,
    )


def make_incomplete(
    deaths: int = 1,
    created_at: str = "2026-01-01T00:00:00",
) -> AttemptRecord:
    """Create an incomplete (death) attempt."""
    return AttemptRecord(
        time_ms=None, completed=False, deaths=deaths,
        clean_tail_ms=None, created_at=created_at,
    )


def make_segment_with_model(
    segment_id: str,
    ms_per_attempt: float = 0.0,
    expected_ms: float = 10000.0,
    floor_ms: float | None = None,
    state_path: str | None = "/fake/state.mss",
    n_completed: int = 5,
    n_attempts: int = 5,
    selected_model: str = "kalman",
    level_number: int = 105,
    start_type: str = "entrance",
    start_ordinal: int = 0,
    end_type: str = "goal",
    end_ordinal: int = 0,
) -> SegmentWithModel:
    """Create a SegmentWithModel for allocator testing."""
    out = ModelOutput(
        total=Estimate(
            expected_ms=expected_ms,
            ms_per_attempt=ms_per_attempt,
            floor_ms=floor_ms,
        ),
        clean=Estimate(),
    )
    return SegmentWithModel(
        segment_id=segment_id,
        game_id="test_game",
        level_number=level_number,
        start_type=start_type,
        start_ordinal=start_ordinal,
        end_type=end_type,
        end_ordinal=end_ordinal,
        description=f"Segment {segment_id}",
        strat_version=1,
        state_path=state_path,
        active=True,
        model_outputs={selected_model: out},
        selected_model=selected_model,
        n_completed=n_completed,
        n_attempts=n_attempts,
    )
