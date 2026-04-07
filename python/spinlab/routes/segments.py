"""Segment CRUD and fill-gap routes."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from spinlab.dashboard import _check_result
from spinlab.db import Database
from spinlab.session_manager import SessionManager

from ._deps import get_db, get_session

router = APIRouter(prefix="/api")


class SegmentPatch(BaseModel):
    is_primary: bool | None = None
    description: str | None = None
    active: bool | None = None


@router.get("/segments")
def api_segments(session: SessionManager = Depends(get_session), db: Database = Depends(get_db)):
    if session.game_id is None:
        return {"segments": []}
    rows = db.get_all_segments_with_model(session.game_id, primary_only=False)
    out = []
    for r in rows:
        start_wp = db.get_waypoint(r["start_waypoint_id"]) if r.get("start_waypoint_id") else None
        end_wp = db.get_waypoint(r["end_waypoint_id"]) if r.get("end_waypoint_id") else None
        r["start_conditions"] = json.loads(start_wp.conditions_json) if start_wp else {}
        r["end_conditions"] = json.loads(end_wp.conditions_json) if end_wp else {}
        r["is_primary"] = bool(r.get("is_primary", 1))
        out.append(r)
    return {"segments": out}


@router.patch("/segments/{segment_id}")
def patch_segment(segment_id: str, body: SegmentPatch, db: Database = Depends(get_db)):
    if not db.segment_exists(segment_id):
        raise HTTPException(status_code=404, detail="segment not found")
    if body.is_primary is not None:
        db.set_segment_is_primary(segment_id, body.is_primary)
    other_fields = body.model_dump(exclude_none=True, exclude={"is_primary"})
    if other_fields:
        db.update_segment(segment_id, **other_fields)
    return {"ok": True, "id": segment_id, "is_primary": body.is_primary}


@router.delete("/segments/{segment_id}")
def delete_segment(segment_id: str, db: Database = Depends(get_db)):
    if not db.segment_exists(segment_id):
        raise HTTPException(status_code=404, detail="Segment not found")
    db.soft_delete_segment(segment_id)
    return {"status": "ok"}


@router.post("/segments/{segment_id}/fill-gap")
async def fill_gap(segment_id: str, session: SessionManager = Depends(get_session)):
    return _check_result(await session.start_fill_gap(segment_id))
