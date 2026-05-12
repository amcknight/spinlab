"""Attempt mutation routes (invalidation toggle)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from spinlab.db import Database

from ._deps import get_db

router = APIRouter(prefix="/api")


class AttemptPatch(BaseModel):
    invalidated: bool


@router.patch("/attempts/{attempt_id}")
def patch_attempt(
    attempt_id: int,
    body: AttemptPatch,
    db: Database = Depends(get_db),
) -> dict:
    """Toggle the invalidation flag on a single attempt."""
    if not db.attempt_exists(attempt_id):
        raise HTTPException(status_code=404, detail="attempt not found")
    db.set_attempt_invalidated(attempt_id, body.invalidated)
    return {"ok": True, "id": attempt_id, "invalidated": body.invalidated}
