"""Attempt mutation routes (invalidation toggle)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from spinlab.api_schemas import AttemptPatchRequest, AttemptPatchResponse, SurgeryAttemptsResponse
from spinlab.db import Database
from spinlab.session_manager import SessionManager

from ._deps import get_db, get_session

router = APIRouter(prefix="/api")


def surgery_rows(attempts: list[dict]) -> list[dict]:
    """Build SurgeryAttempt dicts from Database.get_segment_attempts output.

    Order is chronological (created_at asc, 1-based). is_floor marks the row
    whose clean_tail equals the lowest clean_tail among valid (completed,
    non-invalidated) episodes — matching how compute_golds picks clean_gold.
    """
    ordered = sorted(attempts, key=lambda a: a["created_at"])
    floor_ms = min(
        (a["clean_tail_ms"] for a in ordered
         if a["completed"] and not a["invalidated"] and a["clean_tail_ms"] is not None),
        default=None,
    )
    rows: list[dict] = []
    for i, a in enumerate(ordered, start=1):
        ct = a["clean_tail_ms"]
        rows.append({
            "id": a["id"],
            "order": i,
            "clean_tail_ms": ct,
            "total_ms": a["time_ms"],
            "deaths": a["deaths"],
            "created_at": a["created_at"],
            "completed": bool(a["completed"]),
            "invalidated": bool(a["invalidated"]),
            "is_floor": floor_ms is not None and ct == floor_ms
                        and bool(a["completed"]) and not bool(a["invalidated"]),
        })
    return rows


@router.get(
    "/segments/{segment_id}/attempts",
    response_model=SurgeryAttemptsResponse,
)
def list_segment_attempts(
    segment_id: str,
    db: Database = Depends(get_db),
):
    """Every episode for a segment (incl. invalidated), for the surgery table.

    Distinct from /history (which filters invalidated and omits the id, feeding
    the trend chart). Carries id + invalidated so the row can be toggled.
    """
    raw = db.get_segment_attempts(segment_id)
    return {"segment_id": segment_id, "attempts": surgery_rows(raw)}


@router.patch("/attempts/{attempt_id}", response_model=AttemptPatchResponse)
def patch_attempt(
    attempt_id: int,
    body: AttemptPatchRequest,
    db: Database = Depends(get_db),
    session: SessionManager = Depends(get_session),
):
    """Toggle invalidation on an attempt (episode-level) and recalc its segment.

    set_attempt_invalidated flips the whole episode. We then rebuild that
    segment's model_state so Best/Floor/Room reflect the change immediately
    (Best/gold is computed live; Expected/Floor live in the model_state cache).
    """
    if not db.attempt_exists(attempt_id):
        raise HTTPException(status_code=404, detail="attempt not found")
    db.set_attempt_invalidated(attempt_id, body.invalidated)
    segment_id = db.get_attempt_segment_id(attempt_id)
    if segment_id is not None and session.game_id is not None:
        # update_state_after_episode uses only segment_id (not game_id) to
        # rebuild + save model_state, so the active scheduler can recalc any
        # of its segments. No active game -> skip; the cache refreshes later.
        session.get_scheduler().update_state_after_episode(segment_id)
    return {"ok": True, "id": attempt_id, "invalidated": body.invalidated}
