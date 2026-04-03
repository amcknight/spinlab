"""Segment CRUD and fill-gap routes."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from spinlab.dashboard import _check_result
from spinlab.db import Database
from spinlab.session_manager import SessionManager

from ._deps import get_db, get_session

router = APIRouter(prefix="/api")


@router.get("/segments")
def api_segments(session: SessionManager = Depends(get_session), db: Database = Depends(get_db)):
    segments = db.get_all_segments_with_model(session._require_game())
    return {"segments": segments}


@router.patch("/segments/{segment_id}")
def update_segment_endpoint(segment_id: str, body: dict, db: Database = Depends(get_db)):
    if not db.segment_exists(segment_id):
        raise HTTPException(status_code=404, detail="Segment not found")
    db.update_segment(segment_id, **body)
    return {"status": "ok"}


@router.delete("/segments/{segment_id}")
def delete_segment(segment_id: str, db: Database = Depends(get_db)):
    if not db.segment_exists(segment_id):
        raise HTTPException(status_code=404, detail="Segment not found")
    db.soft_delete_segment(segment_id)
    return {"status": "ok"}


@router.post("/segments/{segment_id}/fill-gap")
async def fill_gap(segment_id: str, session: SessionManager = Depends(get_session)):
    return _check_result(await session.start_fill_gap(segment_id))
