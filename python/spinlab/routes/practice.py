"""Practice start/stop/invalidate routes."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from spinlab.api_schemas import ActionResponse, OkResponse
from spinlab.protocol import AttemptInvalidatedEvent
from spinlab.session_manager import SessionManager

from ._deps import get_session

router = APIRouter(prefix="/api")


@router.post("/practice/start", response_model=ActionResponse)
async def practice_start(session: SessionManager = Depends(get_session)):
    return (await session.start_practice()).to_response()


@router.post("/practice/stop", response_model=ActionResponse)
async def practice_stop(session: SessionManager = Depends(get_session)):
    return (await session.stop_practice()).to_response()


@router.post("/practice/invalidate", response_model=OkResponse)
async def practice_invalidate(session: SessionManager = Depends(get_session)):
    """Mark the current practice attempt as invalidated."""
    await session._handle_attempt_invalidated(AttemptInvalidatedEvent())
    return {"status": "ok"}
