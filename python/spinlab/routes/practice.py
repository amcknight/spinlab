"""Practice start/stop/invalidate routes."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from spinlab.protocol import AttemptInvalidatedEvent
from spinlab.session_manager import SessionManager

from ._deps import get_session

router = APIRouter(prefix="/api")


@router.post("/practice/start")
async def practice_start(session: SessionManager = Depends(get_session)):
    return (await session.start_practice()).to_response()


@router.post("/practice/stop")
async def practice_stop(session: SessionManager = Depends(get_session)):
    return (await session.stop_practice()).to_response()


@router.post("/practice/invalidate")
async def practice_invalidate(session: SessionManager = Depends(get_session)):
    """Mark the current practice attempt as invalidated. Backend-agnostic.

    Delegates to session_manager._handle_attempt_invalidated, which is the
    same handler used for the Mesen-Lua invalidate combo hotkey event. The
    leading underscore reflects internal use; calling it here is intentional
    (the route is a thin shim over a known-shape handler).
    """
    await session._handle_attempt_invalidated(AttemptInvalidatedEvent())
    return {"status": "ok"}
