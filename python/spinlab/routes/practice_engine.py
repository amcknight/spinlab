"""FastAPI routes: /api/practice-engine/state, /api/practice-engine/evaluate.

Read-only diagnostic surface over the PracticeEngine. See
docs/superpowers/specs/2026-06-01-practice-simulation-engine-design.md §11.
"""
from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Depends, HTTPException

from spinlab.api_schemas import (
    PracticeEngineEvaluateRequest,
    PracticeEngineEvaluateResponse,
    PracticeEnginePerSegmentValue,
    PracticeEngineSegmentState,
    PracticeEngineState,
    PracticeEngineTotalTimeSummary,
    PracticeEngineUngated,
)
from spinlab.db import Database
from spinlab.estimators.em_suite_sampler import (
    expected_episode_time_scalar,
    gate_passes,
)
from spinlab.practice_engine import objectives, reset_policies
from spinlab.practice_engine.threshold_sources import thresholds_from_user
from spinlab.routes._deps import get_db, get_session
from spinlab.session_manager import SessionManager

router = APIRouter(prefix="/api/practice-engine")


# Map string identifiers from the request to actual function objects.
# Private module-level (vs. getattr-style indirection) — keeps the surface
# explicit and lets pyright/ruff catch typos at the route level.
_POLICIES: dict[str, Callable] = {
    "no_reset": reset_policies.no_reset,
    "target_paced": reset_policies.target_paced,
}
_OBJECTIVES: dict[str, Callable] = {
    "expected_wall_clock_per_attempt": objectives.expected_wall_clock_per_attempt,
    "expected_total_finished_time": objectives.expected_total_finished_time,
    "q": objectives.q,
    "quantile": objectives.quantile,
    "p_pb_this_session": objectives.p_pb_this_session,
}

# objective_ctx keys each objective indexes directly. Missing them raises KeyError
# deep in the objective (a 500); validate at the boundary and 422 with a clear
# message instead. Objectives absent here need no ctx.
_OBJECTIVE_REQUIRED_CTX: dict[str, list[str]] = {
    "q": ["target_ms"],
    "quantile": ["p"],
    "p_pb_this_session": ["target_ms", "session_remaining_ms"],
}


@router.get("/state", response_model=PracticeEngineState)
def get_state(
    session: SessionManager = Depends(get_session),
    db: Database = Depends(get_db),
) -> PracticeEngineState:
    if session.game_id is None:
        return PracticeEngineState(N=0)
    sched = session.get_scheduler()
    # Route deliberately reaches past the engine to enumerate every persisted
    # sampler row (gated AND ungated). The engine's matrix only knows about
    # gated columns, so it can't surface "needs ≥2 of each" diagnostics.
    states = sched.sampler_states()
    # Scope to the active reference run's traversed segments; no active run ->
    # empty (states without a matching seg entry are skipped below).
    active_run = db.get_active_capture_run(session.game_id)
    segs = (
        {s.id: s for s in db.get_segments_for_run(session.game_id, active_run)}
        if active_run is not None else {}
    )
    golds = db.compute_golds(session.game_id)

    gated: list[PracticeEngineSegmentState] = []
    ungated: list[PracticeEngineUngated] = []

    for seg_id, state in states.items():
        if seg_id not in segs:
            continue
        meta = segs[seg_id]
        gold_ms = golds.get(seg_id, {}).get("gold_ms")
        n_succ = len(state.success_time_pool)
        n_death = len(state.death_time_pool)
        if gate_passes(state):
            # E[sample(0)] from the closed-form scalar; precise per-segment
            # draws live in /evaluate. e_sample_1 is a cheap closed-form
            # placeholder here — /evaluate fills in real draws via
            # PracticeEngine.per_segment_values. None propagates honestly when
            # the scalar refuses to compute (p_die ~= 1).
            e0 = expected_episode_time_scalar(state)
            gated.append(PracticeEngineSegmentState(
                seg_id=seg_id,
                description=meta.description or "",
                level_number=meta.level_number,
                start_type=meta.start_type,
                start_ordinal=meta.start_ordinal,
                end_type=meta.end_type,
                end_ordinal=meta.end_ordinal,
                e_sample_0_ms=e0,
                e_sample_1_ms=e0,
                pool_success=n_succ,
                pool_death=n_death,
                gold_ms=gold_ms,
            ))
        else:
            reason = (
                f"needs >=2 successes (have {n_succ}) and >=2 deaths (have {n_death})"
            )
            ungated.append(PracticeEngineUngated(
                seg_id=seg_id,
                reason=reason,
                description=meta.description or "",
                level_number=meta.level_number,
                start_type=meta.start_type,
                start_ordinal=meta.start_ordinal,
                end_type=meta.end_type,
                end_ordinal=meta.end_ordinal,
            ))

    # Empty gated list → return N=0 without triggering the lazy engine build
    # (no point materializing a zero-column matrix for a no-data game).
    n_value = sched.engine.matrix.N if gated else 0

    return PracticeEngineState(
        gated_segments=gated,
        ungated_segments=ungated,
        matrix_built_at=None,
        N=n_value,
    )


@router.post("/evaluate", response_model=PracticeEngineEvaluateResponse)
def evaluate(
    body: PracticeEngineEvaluateRequest,
    session: SessionManager = Depends(get_session),
    db: Database = Depends(get_db),
) -> PracticeEngineEvaluateResponse:
    if session.game_id is None:
        raise HTTPException(status_code=400, detail="No game loaded")
    sched = session.get_scheduler()
    engine = sched.engine
    engine.matrix.ensure_fresh()

    policy_fn = _POLICIES[body.policy]
    objective_fn = _OBJECTIVES[body.objective]

    missing = [
        k for k in _OBJECTIVE_REQUIRED_CTX.get(body.objective, [])
        if k not in body.objective_ctx
    ]
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"objective {body.objective!r} requires objective_ctx: {', '.join(missing)}",
        )

    threshold_kwargs: dict = {}
    if body.policy == "target_paced":
        cum_splits = body.policy_kwargs.get("cum_splits_ms")
        if cum_splits is None:
            threshold_kwargs["threshold_cum_ms"] = None
        else:
            threshold_kwargs["threshold_cum_ms"] = thresholds_from_user(
                seg_ids=engine.matrix.seg_ids,
                cum_splits_ms=cum_splits,
            )
        threshold_kwargs["slack"] = float(body.policy_kwargs.get("slack", 0.0))

    ctx = dict(body.objective_ctx)

    eval_result = engine.evaluate(policy_fn, threshold_kwargs, objective_fn, ctx)
    per_seg = engine.per_segment_values(policy_fn, threshold_kwargs, objective_fn, ctx)
    dist = engine.total_time_distribution(policy_fn, threshold_kwargs)

    return PracticeEngineEvaluateResponse(
        objective_value=eval_result["value"],
        per_segment_values=[
            PracticeEnginePerSegmentValue(
                seg_id=psv.seg_id,
                value=psv.value,
                value_per_second=psv.value_per_second,
                e_sample_0_ms=psv.e_sample_0_ms,
                e_sample_1_ms=psv.e_sample_1_ms,
            ) for psv in per_seg.values()
        ],
        total_time_summary=PracticeEngineTotalTimeSummary(
            bins=dist["bins"],
            counts=dist["counts"],
            mean=dist["mean"],
            median=dist["median"],
            p10=dist["p10"],
            p90=dist["p90"],
            finished_pct=eval_result["masks_summary"]["finished_pct"],
            aborted_by_segment=eval_result["masks_summary"]["aborted_by_segment"],
        ),
    )
