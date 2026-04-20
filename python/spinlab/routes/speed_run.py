"""Speed Run start/stop routes."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from spinlab.session_manager import SessionManager

from ._deps import get_session

router = APIRouter(prefix="/api")


@router.post("/speedrun/start")
async def speed_run_start(session: SessionManager = Depends(get_session)):
    return (await session.start_speed_run()).to_response()


@router.post("/speedrun/stop")
async def speed_run_stop(session: SessionManager = Depends(get_session)):
    return (await session.stop_speed_run()).to_response()
