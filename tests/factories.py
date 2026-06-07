"""Shared test data factories — consolidates duplicated helpers."""
from __future__ import annotations

from typing import TYPE_CHECKING

from spinlab.allocators import SegmentWithModel
from spinlab.models import Attempt, AttemptRecord, Estimate, ModelOutput, Segment

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
# Spec intent was "attempts across estimators" but estimators are applied
# over history, not per-attempt. Vary allocators instead — the only
# per-attempt diversity knob on the Attempt model.
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
    db.create_capture_run(reference_id, game_id, "FakeRef", kind="live")
    db.promote_draft(reference_id, "FakeRef")
    db.set_active_capture_run(reference_id)

    seg_ids: list[str] = []
    for suffix, level, desc, _ref_ms in _SEED_SEGMENT_SPECS:
        seg_id = f"{game_id}:{suffix}"
        seg_ids.append(seg_id)
        db.upsert_segment(Segment(
            id=seg_id, game_id=game_id, level_number=level,
            start_type="entrance", start_ordinal=0,
            end_type="goal", end_ordinal=0,
            description=desc, capture_run_id=reference_id, active=True,
        ))

    # Stamp a reference-traversal row per segment so each is a genuine MEMBER of
    # the active reference run. Run-scoped model views (Model/Segments/Practice)
    # resolve the active run and filter to its members; without these rows the
    # practice attempts below (which carry no capture_run_id) leave every view
    # empty and the frontend-smoke tests render nothing.
    for seg_id in seg_ids:
        stamp_reference_traversal(db, seg_id, reference_id)

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


def seed_sampler_states(db: "Database") -> dict[str, list[str]]:
    """Add em_suite_sampler-backed segments to the frontend-smoke game so the
    Practice Simulator tab has gated + ungated rows to render.

    Crucially these segments have EMPTY descriptions, so the frontend formats
    their names from the endpoint structure (start/end type+ordinal) — the path
    that regressed to "L… cpundefined → cpundefined". Distinct endpoint types
    exercise shortEndpoint's start/cp/goal branches.

    Returns {"gated": [...], "ungated": [...]} segment ids.
    """
    import json
    from datetime import UTC, datetime

    from spinlab.estimators.em_suite_sampler import (
        EmSuiteSamplerEstimator,
        SamplerState,
        process_event,
    )
    from spinlab.models import AttemptOutcome, AttemptSource, EventAttempt

    game_id = "fake_game_frontend_smoke"
    # Same reference run seed_basic_game activates — the simulator smoke renders
    # these segments only if they are members of the active run, so stamp each
    # with a reference traversal below.
    reference_id = f"{game_id}:ref"
    session_id = f"{game_id}:pe_sess"
    try:
        db.create_session(session_id, game_id)
    except Exception:
        pass  # already exists across a session-scoped reseed

    estimator = EmSuiteSamplerEstimator()

    def _events(seg_id: str, n: int) -> list[EventAttempt]:
        evs = []
        for i in range(n):
            died = i % 2 == 1
            evs.append(EventAttempt(
                segment_id=seg_id, session_id=session_id, episode_id=f"{seg_id}_e{i}",
                outcome=AttemptOutcome.DIED if died else AttemptOutcome.SURVIVED,
                time_ms=1500 if died else 4000 + i * 50,
                source=AttemptSource.PRACTICE, created_at=datetime.now(UTC),
            ))
        return evs

    # (suffix, level, start_type, start_ord, end_type, end_ord, n_events)
    # n_events >= 4 (2 of each) gates; < 4 leaves it ungated.
    specs = [
        ("pe_g1", 201, "entrance", 0, "checkpoint", 1, 8),   # gated → "L201 start → cp1"
        ("pe_g2", 202, "checkpoint", 1, "goal", 0, 8),       # gated → "L202 cp1 → goal"
        ("pe_u1", 203, "entrance", 0, "goal", 0, 2),         # ungated → "L203 start → goal"
    ]
    out: dict[str, list[str]] = {"gated": [], "ungated": []}
    for suffix, level, st, so, et, eo, n in specs:
        seg_id = f"{game_id}:{suffix}"
        db.upsert_segment(Segment(
            id=seg_id, game_id=game_id, level_number=level,
            start_type=st, start_ordinal=so, end_type=et, end_ordinal=eo,
            description="", active=True,
        ))
        events = _events(seg_id, n)
        for ev in events:
            db.log_event_attempt(ev)
        # Membership in the active reference run (run-scoped views filter to it).
        stamp_reference_traversal(db, seg_id, reference_id)
        state = SamplerState()
        for ev in events:
            state = process_event(state, ev)
        output = estimator.model_output(state, [], events=events)
        db.save_model_state(
            seg_id, "em_suite_sampler",
            json.dumps(state.to_dict()), json.dumps(output.to_dict()),
        )
        out["gated" if n >= 4 else "ungated"].append(seg_id)
    return out


def stamp_reference_traversal(
    db: "Database",
    segment_id: str,
    run_id: str,
    *,
    time_ms: int = 1000,
    episode_suffix: str = "reftrav",
    survived: bool = True,
) -> None:
    """Log one non-invalidated REFERENCE event for ``segment_id`` stamped with
    ``run_id``, making the segment a genuine member of that capture run.

    Membership (see ``Database.get_segments_for_run``) is defined as having at
    least one non-invalidated row in the ``attempts`` table carrying the run's
    ``capture_run_id``. Run-scoped model views resolve the active run and then
    filter to its members, so a fixture that seeds segments + sets an active run
    must also stamp traversal rows or the views render empty.

    ``episode_suffix`` distinguishes multiple traversals of the same segment by
    the same run; rows sharing an ``episode_id`` roll up into one episode.

    ``survived=False`` logs a DIED episode — still a non-invalidated member, but
    NOT a completed attempt, so it is invisible to segment-history's completed
    filter. Use it when a fixture needs membership without adding a completed
    attempt that would perturb history/count assertions.
    """
    from datetime import UTC, datetime

    from spinlab.models import AttemptOutcome, AttemptSource, EventAttempt

    db.log_event_attempt(EventAttempt(
        segment_id=segment_id,
        episode_id=f"{run_id}:{segment_id}:{episode_suffix}",
        outcome=AttemptOutcome.SURVIVED if survived else AttemptOutcome.DIED,
        time_ms=time_ms,
        capture_run_id=run_id, source=AttemptSource.REFERENCE,
        created_at=datetime.now(UTC),
    ))


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


def make_capture_session(
    db: "Database",
    run_id: str,
    ordinal: int = 1,
    session_id: str | None = None,
) -> str:
    import uuid
    sid = session_id or f"sess_{uuid.uuid4().hex[:8]}"
    db.create_capture_session(sid, run_id, ordinal)
    return sid


def make_event_attempt(
    segment_id: str = "seg1",
    episode_id: str = "ep1",
    outcome: str = "died",
    time_ms: int = 5000,
    session_id: str | None = "_default_test_session",
    capture_run_id: str | None = None,
    invalidated: bool = False,
    is_hot: bool = False,
    created_at: str = "2026-01-01T00:00:00",
):
    """Create an EventAttempt for unit tests."""
    from datetime import datetime
    from spinlab.models import AttemptOutcome, AttemptSource, EventAttempt
    if session_id is None and capture_run_id is None:
        capture_run_id = "_default_test_capture_run"
    return EventAttempt(
        segment_id=segment_id,
        episode_id=episode_id,
        outcome=AttemptOutcome(outcome),
        time_ms=time_ms,
        session_id=session_id,
        capture_run_id=capture_run_id,
        source=AttemptSource.PRACTICE,
        invalidated=invalidated,
        is_hot=is_hot,
        created_at=datetime.fromisoformat(created_at),
    )


def make_segment_with_model(
    segment_id: str,
    ms_per_attempt: float = 0.0,
    expected_ms: float = 10000.0,
    floor_ms: float | None = None,
    state_path: str | None = "/fake/state.mss",
    n_completed: int = 5,
    n_attempts: int = 5,
    selected_model: str = "em_suite_sampler",
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
        state_path=state_path,
        active=True,
        model_outputs={selected_model: out},
        selected_model=selected_model,
        n_completed=n_completed,
        n_attempts=n_attempts,
    )
