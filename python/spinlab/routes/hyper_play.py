"""Hyper Play start/stop routes."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from spinlab.api_schemas import ActionResponse
from spinlab.session_manager import SessionManager

from ._deps import get_session

router = APIRouter(prefix="/api")


@router.post("/hyperplay/start", response_model=ActionResponse)
async def hyper_play_start(session: SessionManager = Depends(get_session)):
    return (await session.start_hyper_play()).to_response()


@router.post("/hyperplay/stop", response_model=ActionResponse)
async def hyper_play_stop(session: SessionManager = Depends(get_session)):
    return (await session.stop_hyper_play()).to_response()
