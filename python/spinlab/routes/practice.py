"""Practice start/stop routes."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from spinlab.session_manager import SessionManager

from ._deps import get_session

router = APIRouter(prefix="/api")


@router.post("/practice/start")
async def practice_start(session: SessionManager = Depends(get_session)):
    return (await session.start_practice()).to_response()


@router.post("/practice/stop")
async def practice_stop(session: SessionManager = Depends(get_session)):
    return (await session.stop_practice()).to_response()
