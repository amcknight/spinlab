"""Shared test data factories — consolidates duplicated helpers."""
from __future__ import annotations

from typing import TYPE_CHECKING

from spinlab.models import Attempt, AttemptRecord, Estimate, ModelOutput, Segment
from spinlab.allocators import SegmentWithModel

if TYPE_CHECKING:
    from spinlab.db import Database


# -- Frontend-smoke seed helpers ---------------------------------------------
#
# These constants feed seed_basic_game. They are chosen to yield a non-empty
# render on every dashboard tab (practice / segments / manage / model) without
# depending on a live emulator.

# Three realistic SMW-style segments with distinct level numbers so the
# segments tab has something to list. Times span "fast / medium / slow" to
# exercise rendering that buckets by duration.
_SEED_GAME_NAME = "FakeGame"
_SEED_CATEGORY = "any%"
_SEED_SEGMENT_SPECS: tuple[tuple[str, int, str, int], ...] = (
    # (segment_id_suffix, level_number, description, reference_time_ms)
    ("s1", 101, "Fake Level 1",  4_500),
    ("s2", 102, "Fake Level 2",  7_200),
    ("s3", 103, "Fake Level 3", 11_000),
)

# Two allocators so the model tab's allocator-color legend has >1 bucket.
# Values must match real allocator identifiers in spinlab.allocators.
_SEED_ALLOCATORS: tuple[str, str] = ("greedy", "least_played")

# Attempt spread: (segment_idx, time_ms_offset, completed, allocator_idx).
# 10 attempts across 3 segments, 2 allocators, with one death to populate
# the incomplete-attempt code path.
_SEED_ATTEMPT_SPECS: tuple[tuple[int, int, bool, int], ...] = (
    (0,    0, True,  0),
    (0,  200, True,  1),
    (0, -300, True,  0),
    (1,    0, True,  0),
    (1,  400, True,  1),
    (1, -100, True,  0),
    (2,    0, True,  1),
    (2,  500, True,  0),
    (2, -200, True,  1),
    (2,    0, False, 0),  # death
)


def seed_basic_game(db: "Database") -> str:
    """Insert a minimal game with segments, reference, attempts, and a session.

    Returns the seeded game_id. Idempotent per (game_id, reference_id) pair.
    Intended for the `fake_game_loaded` frontend-smoke fixture.
    """
    game_id = "fake_game_frontend_smoke"
    reference_id = f"{game_id}:ref"
    session_id = f"{game_id}:sess"

    db.upsert_game(game_id, _SEED_GAME_NAME, _SEED_CATEGORY)
    db.create_capture_run(reference_id, game_id, "FakeRef", draft=False)
    db.set_active_capture_run(reference_id)

    seg_ids: list[str] = []
    for suffix, level, desc, _ref_ms in _SEED_SEGMENT_SPECS:
        seg_id = f"{game_id}:{suffix}"
        seg_ids.append(seg_id)
        db.upsert_segment(Segment(
            id=seg_id, game_id=game_id, level_number=level,
            start_type="entrance", start_ordinal=0,
            end_type="goal", end_ordinal=0,
            description=desc, reference_id=reference_id, active=True,
        ))

    db.create_session(session_id, game_id)
    for seg_idx, offset_ms, completed, alloc_idx in _SEED_ATTEMPT_SPECS:
        _, _, _, ref_ms = _SEED_SEGMENT_SPECS[seg_idx]
        db.log_attempt(Attempt(
            segment_id=seg_ids[seg_idx], session_id=session_id,
            completed=completed,
            time_ms=(ref_ms + offset_ms) if completed else None,
            chosen_allocator=_SEED_ALLOCATORS[alloc_idx],
        ))

    return game_id


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
