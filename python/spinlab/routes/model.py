"""Model state, allocator weights, and estimator routes."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

logger = logging.getLogger(__name__)

from spinlab.api_schemas import (
    AllocatorWeightsResponse,
    EmSuiteMatrixResponse,
    LiveSegmentViewResponse,
    ModelData,
    RouteSummaryResponse,
    SegmentHistory,
    SegmentProgressResponse,
)
from spinlab.cold_distribution import compute_cold_distribution
from spinlab.db import Database
from spinlab.scheduler import _attempts_from_rows, _events_from_rows
from spinlab.session_manager import SessionManager

from ._deps import get_db, get_session

router = APIRouter(prefix="/api")


@router.get("/model", response_model=ModelData)
def api_model(session: SessionManager = Depends(get_session)):
    if session.game_id is None:
        return {"estimator": None, "allocator_weights": None, "segments": []}
    sched = session.get_scheduler()
    segments = sched.get_all_model_states()
    return {
        "estimator": sched.estimator.name,
        "allocator_weights": sched.all_weights,
        "segments": [
            {
                "segment_id": s.segment_id,
                "description": s.description,
                "level_number": s.level_number,
                "start_type": s.start_type,
                "start_ordinal": s.start_ordinal,
                "end_type": s.end_type,
                "end_ordinal": s.end_ordinal,
                "selected_model": s.selected_model,
                "model_outputs": {
                    name: out.to_dict()
                    for name, out in s.model_outputs.items()
                },
                "n_completed": s.n_completed,
                "n_attempts": s.n_attempts,
                "gold_ms": s.gold_ms,
                "clean_gold_ms": s.clean_gold_ms,
            }
            for s in segments
        ],
    }


@router.post("/allocator-weights", response_model=AllocatorWeightsResponse)
def set_allocator_weights(body: dict[str, int], session: SessionManager = Depends(get_session)):
    sched = session.get_scheduler()
    try:
        sched.set_allocator_weights(body)
    except (ValueError, TypeError) as e:
        logger.warning("set_allocator_weights: %s", e)
        raise HTTPException(status_code=400, detail=str(e))
    return {"weights": body}


@router.get("/segments/{segment_id}/history", response_model=SegmentHistory)
def segment_history(
    segment_id: str,
    db: Database = Depends(get_db),
    session: SessionManager = Depends(get_session),
):
    seg = db.get_segment_by_id(segment_id)
    if seg is None:
        logger.warning("segment_history: unknown segment %r", segment_id)
        raise HTTPException(status_code=404, detail=f"Segment not found: {segment_id}")

    raw_rows = db.get_segment_attempts(segment_id)
    # _attempts_from_rows already drops invalidated; filter to completed too.
    all_records = _attempts_from_rows(raw_rows)
    completed = [a for a in all_records if a.completed and a.time_ms is not None]

    # Load events once for the sampler.
    event_rows = db.get_segment_event_rows(segment_id)
    events = _events_from_rows(event_rows)

    attempts = []
    for i, a in enumerate(completed):
        attempts.append({
            "attempt_number": i + 1,
            "time_ms": a.time_ms,
            "clean_tail_ms": a.clean_tail_ms,
            "deaths": a.deaths,
            "created_at": a.created_at,
        })

    from spinlab.estimators.em_suite_sampler import EmSuiteSamplerEstimator
    est = EmSuiteSamplerEstimator()
    final_state = est.rebuild_state(all_records, events=events)
    final_out = est.model_output(final_state, completed, events=events)
    estimator_curves: dict[str, dict] = {
        est.name: {
            "total": {"expected_ms": [], "floor_ms": []},
            "clean": {"expected_ms": [], "floor_ms": []},
            "final_extras": (
                final_out.extras.to_dict() if final_out.extras is not None else None
            ),
        }
    }

    sched = session.get_scheduler() if session.game_id is not None else None
    selected_model = sched.estimator.name if sched is not None else None

    # Cold-only distribution for the segment-detail panel (histogram + hazard).
    # v0: equal-weighted raw empirical view (no recency knob). See
    # docs/superpowers/specs/2026-06-01-model-purge-sampler-core-design.md.
    cold_events = [ev for ev in events if not ev.is_hot]
    cold_distribution = (
        compute_cold_distribution(cold_events) if cold_events else None
    )

    return {
        "segment_id": segment_id,
        "description": seg.description,
        "level_number": seg.level_number,
        "start_type": seg.start_type,
        "start_ordinal": seg.start_ordinal,
        "end_type": seg.end_type,
        "end_ordinal": seg.end_ordinal,
        "attempts": attempts,
        "estimator_curves": estimator_curves,
        "selected_model": selected_model,
        "cold_distribution": cold_distribution,
    }


@router.get(
    "/segments/{segment_id}/em-suite-matrix",
    response_model=EmSuiteMatrixResponse,
)
def get_em_suite_matrix(
    segment_id: str,
    db: Database = Depends(get_db),
):
    """Per-segment EMA-suite prediction matrix.

    Replays the segment's event log through the EmSuiteSamplerEstimator,
    computes the closed-form geometric mean per (alpha_fast, alpha_slow)
    pair. See docs/superpowers/specs/2026-05-30-em-suite-sampler-design.md.
    """
    from spinlab.estimators.em_suite_sampler import (
        build_matrix,
        build_slope_matrices,
        replay_with_history,
    )

    seg = db.get_segment_by_id(segment_id)
    if seg is None:
        logger.warning("get_em_suite_matrix: unknown segment %r", segment_id)
        raise HTTPException(status_code=404, detail=f"Segment not found: {segment_id}")

    event_rows = db.get_segment_event_rows(segment_id)
    events = _events_from_rows(event_rows)
    # replay_with_history both produces the final state and the per-snapshot
    # history (drives the time-series view); no need to also call rebuild_state.
    state, param_history = replay_with_history(events)
    grid = build_matrix(state)
    slope_matrices = build_slope_matrices(state)
    return {
        "segment_id": segment_id,
        "alpha_grid": grid["alpha_grid"],
        "baseline": grid["baseline"],
        "matrix": grid["matrix"],
        "n_attempts_total": state.n_attempts_total,
        "n_successes": state.n_successes,
        "n_deaths": state.n_deaths,
        "param_history": param_history,
        "slope_matrices": slope_matrices,
    }


@router.get(
    "/segments/{segment_id}/progress",
    response_model=SegmentProgressResponse,
)
def get_segment_progress(
    segment_id: str,
    db: Database = Depends(get_db),
):
    """'Am I improving on this segment?' — recent-vs-baseline clear time,
    death rate, consistency, gap-to-gold, and the recent clear-time trend.
    Replays the event log through the sampler; pure read."""
    from spinlab.estimators.em_suite_sampler import replay_with_history
    from spinlab.estimators.segment_progress import segment_progress

    seg = db.get_segment_by_id(segment_id)
    if seg is None:
        logger.warning("get_segment_progress: unknown segment %r", segment_id)
        raise HTTPException(status_code=404, detail=f"Segment not found: {segment_id}")

    events = _events_from_rows(db.get_segment_event_rows(segment_id))
    state, _history = replay_with_history(events)
    gold_row = db.compute_golds(seg.game_id).get(segment_id)
    gold_ms = gold_row["gold_ms"] if gold_row else None
    p = segment_progress(state, gold_ms=gold_ms)
    return {
        "segment_id": segment_id,
        "ready": p.ready,
        "verdict": p.verdict,
        "now_clear_ms": p.now_clear_ms,
        "baseline_clear_ms": p.baseline_clear_ms,
        "death_rate": p.death_rate,
        "consistency_ms": p.consistency_ms,
        "gap_to_gold_ms": p.gap_to_gold_ms,
        "pb_ms": p.pb_ms,
        "trend_ms": p.trend_ms,
        "n_successes": state.n_successes,
        "n_deaths": state.n_deaths,
    }


@router.get("/segments/{segment_id}/live", response_model=LiveSegmentViewResponse)
def get_segment_live(
    segment_id: str,
    db: Database = Depends(get_db),
    session: SessionManager = Depends(get_session),
):
    """Closed-form live-view payload for one segment (episode-time trend, floor,
    expected, practice gain, deaths). Pure read; no Monte-Carlo. Diff fields are
    populated only when a practice session is active."""
    from spinlab.estimators.em_suite_sampler import replay_with_history
    from spinlab.estimators.live_view import live_segment_view

    seg = db.get_segment_by_id(segment_id)
    if seg is None:
        logger.warning("get_segment_live: unknown segment %r", segment_id)
        raise HTTPException(status_code=404, detail=f"Segment not found: {segment_id}")

    events = _events_from_rows(db.get_segment_event_rows(segment_id))
    state, _history = replay_with_history(events)
    episodes = db.get_segment_attempts(segment_id)

    snap = session.practice_session_snapshot
    baseline = snap.segments.get(segment_id) if snap is not None else None
    v = live_segment_view(state, episodes, baseline=baseline)

    return {
        "segment_id": segment_id,
        "ready": v.ready,
        "expected_episode_ms": v.expected_episode_ms,
        "practice_gain_ms": v.practice_gain_ms,
        "death_rate": v.death_rate,
        "floor_ms": v.floor_ms,
        "last_episode_ms": v.last_episode_ms,
        "last_clean_ms": v.last_clean_ms,
        "last_deaths": v.last_deaths,
        "last_rank": v.last_rank,
        "series": v.series,
        "n_successes": state.n_successes,
        "n_deaths": state.n_deaths,
        "expected_episode_diff_ms": v.expected_episode_diff_ms,
        "practice_gain_diff_ms": v.practice_gain_diff_ms,
        "floor_diff_ms": v.floor_diff_ms,
        "death_rate_diff": v.death_rate_diff,
    }


@router.get("/games/{game_id}/live-summary", response_model=RouteSummaryResponse)
def get_route_summary(
    game_id: str,
    db: Database = Depends(get_db),
    session: SessionManager = Depends(get_session),
):
    """Closed-form whole-run aggregate for the route bar. Pure read; no MC.
    Session-overlay fields populated only when a practice session is active."""
    from spinlab.estimators.em_suite_sampler import replay_with_history
    from spinlab.estimators.live_view import route_summary

    snap = session.practice_session_snapshot

    states = []
    floor_improvement_ms: float | None = None
    if snap is not None:
        floor_improvement_ms = 0.0
    for seg in db.get_active_segments(game_id):
        events = _events_from_rows(db.get_segment_event_rows(seg.id))
        state, _history = replay_with_history(events)
        states.append(state)
        # Aggregate floor improvement vs baseline. Per-segment improvement =
        # max(0, baseline_floor - current_running_min_clean). None on either side -> skip.
        # max(0, ...) because a new floor only ever drops; clipping to 0 prevents
        # paradoxical "regression" if an invalidation flips the running-min back up.
        if snap is not None and floor_improvement_ms is not None:
            base = snap.segments.get(seg.id)
            if base is not None and base.floor_ms is not None:
                episodes = db.get_segment_attempts(seg.id)
                cur = _running_min_clean_for_route(episodes)
                if cur is not None:
                    floor_improvement_ms += max(0.0, base.floor_ms - cur)

    s = route_summary(states, baseline=snap.route if snap else None)
    return {
        "game_id": game_id,
        "exp_run_ms": s.exp_run_ms,
        "exp_deaths": s.exp_deaths,
        "n_estimable": s.n_estimable,
        "n_skipped": s.n_skipped,
        "session_started_at": snap.started_at if snap else None,
        "exp_run_diff_ms": s.exp_run_diff_ms,
        "exp_deaths_diff": s.exp_deaths_diff,
        "practice_saved_ms": s.practice_saved_ms,
        "floor_improvement_ms": floor_improvement_ms,
    }


def _running_min_clean_for_route(episodes):
    """Helper for the route-bar floor_improvement aggregation. Same scan as
    session_snapshot._running_min_clean (intentional duplication while the
    three call sites stabilize; consolidate in a later cleanup pass)."""
    floor: float | None = None
    for e in episodes:
        if not e.get("completed") or e.get("invalidated"):
            continue
        clean = e.get("clean_tail_ms")
        if clean is None:
            continue
        floor = float(clean) if floor is None else min(floor, float(clean))
    return floor
