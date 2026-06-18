"""Practice start/stop/invalidate routes."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from spinlab.api_schemas import ActionResponse, GrindStartRequest, OkResponse
from spinlab.session_manager import SessionManager

from ._deps import get_session

router = APIRouter(prefix="/api")


@router.post("/practice/start", response_model=ActionResponse)
async def practice_start(session: SessionManager = Depends(get_session)):
    return (await session.start_practice()).to_response()


@router.post("/practice/grind", response_model=ActionResponse)
async def practice_grind(
    req: GrindStartRequest, session: SessionManager = Depends(get_session)
):
    """Start practice pinned to one segment (GrindOne) — repeat it every cycle."""
    return (await session.start_practice(grind_segment_id=req.segment_id)).to_response()


@router.post("/practice/stop", response_model=ActionResponse)
async def practice_stop(session: SessionManager = Depends(get_session)):
    return (await session.stop_practice()).to_response()


@router.post("/practice/invalidate", response_model=OkResponse)
async def practice_invalidate(session: SessionManager = Depends(get_session)):
    """Mark the current practice attempt as invalidated."""
    await session.invalidate_current_attempt()
    return {"status": "ok"}


@router.post("/practice/science", response_model=OkResponse)
async def practice_science(session: SessionManager = Depends(get_session)):
    """Toggle Science/no-record mode on the live practice session."""
    session.toggle_experimental()
    return {"status": "ok"}
