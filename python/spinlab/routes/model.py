"""Model state, allocator weights, and estimator routes."""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException

logger = logging.getLogger(__name__)

from spinlab.api_schemas import (
    AllocatorWeightsResponse,
    EmSuiteMatrixResponse,
    EstimatorParamsRequest,
    EstimatorSwitchRequest,
    EstimatorSwitchResponse,
    ModelData,
    OkResponse,
    SegmentHistory,
    TuningData,
)
from spinlab.cold_distribution import compute_cold_distribution
from spinlab.db import Database
from spinlab.estimators import get_estimator, list_estimators
from spinlab.estimators.death_aware_rolling import _resolve_halflife
from spinlab.scheduler import _attempts_from_rows, _events_from_rows
from spinlab.session_manager import SessionManager

from ._deps import get_db, get_session

router = APIRouter(prefix="/api")


@router.get("/model", response_model=ModelData)
def api_model(session: SessionManager = Depends(get_session)):
    if session.game_id is None:
        return {"estimator": None, "estimators": [], "allocator_weights": None, "segments": []}
    sched = session.get_scheduler()
    segments = sched.get_all_model_states()
    return {
        "estimator": sched.estimator.name,
        "estimators": [
            {"name": n, "display_name": get_estimator(n).display_name or n}
            for n in list_estimators()
        ],
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


@router.post("/estimator", response_model=EstimatorSwitchResponse)
def switch_estimator(body: EstimatorSwitchRequest, session: SessionManager = Depends(get_session)):
    from spinlab.estimators import list_estimators
    valid = list_estimators()
    if body.name not in valid:
        logger.warning("switch_estimator: unknown %r (valid: %s)", body.name, valid)
        raise HTTPException(status_code=400, detail=f"Unknown estimator: {body.name}. Valid: {valid}")
    sched = session.get_scheduler()
    sched.switch_estimator(body.name)
    return {"estimator": body.name}


@router.get("/estimator-params", response_model=TuningData)
def get_estimator_params(session: SessionManager = Depends(get_session), db: Database = Depends(get_db)):
    if session.game_id is None:
        return {"estimator": None, "params": []}
    sched = session.get_scheduler()
    est = sched.estimator
    declared = est.declared_params()
    raw = db.load_allocator_config(f"estimator_params:{est.name}")
    saved = json.loads(raw) if raw else {}
    return {
        "estimator": est.name,
        "params": [
            {
                **p.to_dict(),
                "value": saved.get(p.name, p.default),
            }
            for p in declared
        ],
    }


@router.post("/estimator-params", response_model=OkResponse)
def set_estimator_params(
    body: EstimatorParamsRequest,
    session: SessionManager = Depends(get_session),
    db: Database = Depends(get_db),
):
    sched = session.get_scheduler()
    est = sched.estimator
    valid_names = {p.name for p in est.declared_params()}
    for name in body.params:
        if name not in valid_names:
            logger.warning("set_estimator_params: unknown param %r (valid: %s)", name, valid_names)
            raise HTTPException(status_code=400, detail=f"Unknown param: {name}")
    db.save_allocator_config(f"estimator_params:{est.name}", json.dumps(body.params))
    sched.rebuild_all_states()
    return {"status": "ok"}


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

    # Load events once for death-aware estimators. They produce DeathExtras
    # from events, not from AttemptRecords. Estimators that ignore events
    # (rolling_mean, kalman) get this argument harmlessly.
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

    estimator_names = list_estimators()
    estimator_curves: dict[str, dict] = {}

    for est_name in estimator_names:
        est = get_estimator(est_name)
        saved_raw = db.load_allocator_config(f"estimator_params:{est_name}")
        params = json.loads(saved_raw) if saved_raw else None
        priors = est.get_priors(db, seg.game_id)

        total_expected: list[float | None] = []
        total_floor: list[float | None] = []
        clean_expected: list[float | None] = []
        clean_floor: list[float | None] = []

        final_extras = None
        if completed:
            state = est.init_state(completed[0], priors, params=params)
            out = est.model_output(state, completed[:1], params=params)
            total_expected.append(out.total.expected_ms)
            total_floor.append(out.total.floor_ms)
            clean_expected.append(out.clean.expected_ms)
            clean_floor.append(out.clean.floor_ms)

            for j in range(1, len(completed)):
                state = est.process_attempt(
                    state, completed[j], completed[:j + 1], params=params,
                )
                out = est.model_output(state, completed[:j + 1], params=params)
                total_expected.append(out.total.expected_ms)
                total_floor.append(out.total.floor_ms)
                clean_expected.append(out.clean.expected_ms)
                clean_floor.append(out.clean.floor_ms)

            # Compute final extras by reusing the final state from the
            # per-attempt loop above and recomputing model_output with the
            # full event list. The per-attempt loop intentionally doesn't
            # pass events (the per-attempt curves for death-aware
            # estimators are pre-existing-empty; out of scope here).
            # Estimators that don't publish extras (kalman, rolling_mean)
            # return extras=None naturally — no explicit gate needed.
            final_out = est.model_output(
                state, completed, params=params, events=events,
            )
            final_extras = (
                final_out.extras.to_dict() if final_out.extras is not None else None
            )

        estimator_curves[est_name] = {
            "total": {"expected_ms": total_expected, "floor_ms": total_floor},
            "clean": {"expected_ms": clean_expected, "floor_ms": clean_floor},
            "final_extras": final_extras,
        }

    sched = session.get_scheduler() if session.game_id is not None else None
    selected_model = sched.estimator.name if sched is not None else None

    # Cold-only distribution for the segment-detail panel (histogram + hazard).
    # Use the active death_aware_rolling halflife so the cold panel tracks the
    # user's tuned smoothing knob (shared with DAR + bootstrap).
    cold_events = [ev for ev in events if not ev.is_hot]
    if cold_events:
        dar_params_raw = db.load_allocator_config("estimator_params:death_aware_rolling")
        dar_params = json.loads(dar_params_raw) if dar_params_raw else None
        halflife = _resolve_halflife(dar_params)
        cold_distribution = compute_cold_distribution(cold_events, halflife=halflife)
    else:
        cold_distribution = None

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
