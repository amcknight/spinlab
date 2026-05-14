"""Attempt mutation routes (invalidation toggle)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from spinlab.api_schemas import AttemptPatchRequest, AttemptPatchResponse
from spinlab.db import Database

from ._deps import get_db

router = APIRouter(prefix="/api")


@router.patch("/attempts/{attempt_id}", response_model=AttemptPatchResponse)
def patch_attempt(
    attempt_id: int,
    body: AttemptPatchRequest,
    db: Database = Depends(get_db),
):
    """Toggle the invalidation flag on a single attempt."""
    if not db.attempt_exists(attempt_id):
        raise HTTPException(status_code=404, detail="attempt not found")
    db.set_attempt_invalidated(attempt_id, body.invalidated)
    return {"ok": True, "id": attempt_id, "invalidated": body.invalidated}
