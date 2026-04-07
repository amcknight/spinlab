"""Model state, allocator weights, and estimator routes."""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException

logger = logging.getLogger(__name__)

from spinlab.dashboard import _check_result
from spinlab.db import Database
from spinlab.estimators import get_estimator, list_estimators
from spinlab.session_manager import SessionManager

from ._deps import get_db, get_session

router = APIRouter(prefix="/api")


@router.get("/model")
def api_model(session: SessionManager = Depends(get_session)):
    if session.game_id is None:
        return {"estimator": None, "estimators": [], "allocator_weights": None, "segments": []}
    sched = session._get_scheduler()
    segments = sched.get_all_model_states()
    return {
        "estimator": sched.estimator.name,
        "estimators": [
            {"name": n, "display_name": get_estimator(n).display_name or n}
            for n in list_estimators()
        ],
        "allocator_weights": {alloc.name: int(w) for alloc, w in sched.allocator.entries},
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


@router.post("/allocator-weights")
def set_allocator_weights(body: dict, session: SessionManager = Depends(get_session)):
    sched = session._get_scheduler()
    try:
        sched.set_allocator_weights(body)
    except (ValueError, TypeError) as e:
        logger.warning("set_allocator_weights: %s", e)
        raise HTTPException(status_code=400, detail=str(e))
    return {"weights": body}


@router.post("/estimator")
def switch_estimator(body: dict, session: SessionManager = Depends(get_session)):
    from spinlab.estimators import list_estimators
    name = body.get("name")
    valid = list_estimators()
    if name not in valid:
        logger.warning("switch_estimator: unknown %r (valid: %s)", name, valid)
        raise HTTPException(status_code=400, detail=f"Unknown estimator: {name}. Valid: {valid}")
    sched = session._get_scheduler()
    sched.switch_estimator(name)
    return {"estimator": name}


@router.get("/estimator-params")
def get_estimator_params(session: SessionManager = Depends(get_session), db: Database = Depends(get_db)):
    if session.game_id is None:
        return {"estimator": None, "params": []}
    sched = session._get_scheduler()
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


@router.post("/estimator-params")
def set_estimator_params(body: dict, session: SessionManager = Depends(get_session), db: Database = Depends(get_db)):
    sched = session._get_scheduler()
    est = sched.estimator
    params = body.get("params", {})
    # Validate param names
    valid_names = {p.name for p in est.declared_params()}
    for name in params:
        if name not in valid_names:
            logger.warning("set_estimator_params: unknown param %r (valid: %s)", name, valid_names)
            raise HTTPException(status_code=400, detail=f"Unknown param: {name}")
    db.save_allocator_config(f"estimator_params:{est.name}", json.dumps(params))
    sched.rebuild_all_states()
    return {"status": "ok"}
