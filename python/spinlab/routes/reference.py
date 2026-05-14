"""Reference CRUD, drafts, and replay routes."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from spinlab.api_schemas import (
    ActionResponse,
    CaptureSessionsResponse,
    OkResponse,
    ReferenceFinalizeRequest,
    ReferenceRenameRequest,
    ReferenceSegmentsResponse,
    ReferencesResponse,
    ReplayExistsResponse,
    ReplayStartRequest,
)
from spinlab.db import Database
from spinlab.session_manager import SessionManager

from ._deps import get_db, get_session

router = APIRouter(prefix="/api")


@router.post("/reference/start", response_model=ActionResponse)
async def reference_start(session: SessionManager = Depends(get_session)):
    return (await session.start_reference()).to_response()


@router.post("/reference/stop", response_model=ActionResponse)
async def reference_stop(session: SessionManager = Depends(get_session)):
    return (await session.stop_reference()).to_response()


def _resolve_replay_path(
    ref_id: str,
    session: "SessionManager",
    db: Database,
    session_id: str | None = None,
) -> str | None:
    """Return the .replay path for a reference run. Returns None if no
    replay file exists for the requested ref/session."""
    gid = session.game_id or "unknown"
    sessions = db.list_capture_sessions_for_run(ref_id)
    if sessions:
        if session_id is not None:
            target = next((s for s in sessions if s["id"] == session_id), None)
            if target is None:
                return None
            ordinal = target["ordinal"]
        else:
            ordinal = sessions[0]["ordinal"]
        path = session.data_dir / gid / "rec" / f"{ref_id}__sess{ordinal:03d}.replay"
    else:
        path = session.data_dir / gid / "rec" / f"{ref_id}.replay"
    return str(path) if path.is_file() else None


@router.post("/replay/start", response_model=ActionResponse)
async def replay_start(
    body: ReplayStartRequest,
    session: SessionManager = Depends(get_session),
    db: Database = Depends(get_db),
):
    replay_path = _resolve_replay_path(body.ref_id, session, db, session_id=body.session_id)
    if not replay_path:
        raise HTTPException(status_code=404, detail="replay_not_found")
    return (await session.start_replay(replay_path, speed=body.speed)).to_response()


@router.post("/replay/stop", response_model=ActionResponse)
async def replay_stop(session: SessionManager = Depends(get_session)):
    return (await session.stop_replay()).to_response()


@router.get("/references", response_model=ReferencesResponse)
def list_references(session: SessionManager = Depends(get_session), db: Database = Depends(get_db)):
    if session.game_id is None:
        return {"references": []}
    gid = session.game_id
    refs = db.list_capture_runs(gid)
    out: list[dict] = []
    for ref in refs:
        d: dict = dict(ref)
        ref_id = d["id"]
        sessions = db.list_capture_sessions_for_run(ref_id)
        if sessions:
            d["has_replay"] = any(
                (session.data_dir / gid / "rec" / f"{ref_id}__sess{s['ordinal']:03d}.replay").is_file()
                for s in sessions
            )
        else:
            legacy_replay = session.data_dir / gid / "rec" / f"{ref_id}.replay"
            d["has_replay"] = legacy_replay.is_file()
        out.append(d)
    return {"references": out}


@router.post("/reference/finalize", response_model=ActionResponse)
async def reference_finalize(
    body: ReferenceFinalizeRequest, session: SessionManager = Depends(get_session),
):
    return (await session.finalize_run(body.name)).to_response()


@router.post("/reference/save_and_finish", response_model=ActionResponse)
async def reference_save_and_finish(
    body: ReferenceFinalizeRequest, session: SessionManager = Depends(get_session),
):
    return (await session.save_and_finish_run(body.name)).to_response()


@router.post("/reference/discard_run", response_model=ActionResponse)
async def reference_discard_run(session: SessionManager = Depends(get_session)):
    return (await session.discard_run()).to_response()


@router.post("/reference/resume", response_model=ActionResponse)
async def reference_resume(session: SessionManager = Depends(get_session)):
    return (await session.resume_reference()).to_response()


@router.delete("/capture_sessions/{session_id}", response_model=ActionResponse)
async def delete_capture_session(session_id: str, session: SessionManager = Depends(get_session)):
    return (await session.delete_capture_session(session_id)).to_response()


@router.get("/capture_sessions", response_model=CaptureSessionsResponse)
def list_capture_sessions(
    run_id: str,
    session: SessionManager = Depends(get_session),
    db: Database = Depends(get_db),
):
    return {"sessions": db.list_capture_sessions_for_run(run_id)}


@router.get("/references/{ref_id}/replay", response_model=ReplayExistsResponse)
def check_replay(ref_id: str, session: SessionManager = Depends(get_session), db: Database = Depends(get_db)):
    replay_path = _resolve_replay_path(ref_id, session, db)
    if replay_path:
        return {"exists": True, "path": replay_path}
    return {"exists": False}


@router.patch("/references/{ref_id}", response_model=OkResponse)
def rename_reference(
    ref_id: str, body: ReferenceRenameRequest, db: Database = Depends(get_db),
):
    if body.name:
        db.rename_capture_run(ref_id, body.name)
    return {"status": "ok"}


@router.delete("/references/{ref_id}", response_model=OkResponse)
def delete_reference(ref_id: str, db: Database = Depends(get_db)):
    db.delete_capture_run(ref_id)
    return {"status": "ok"}


@router.post("/references/{ref_id}/activate", response_model=OkResponse)
def activate_reference(ref_id: str, db: Database = Depends(get_db)):
    db.set_active_capture_run(ref_id)
    return {"status": "ok"}


@router.get("/references/{ref_id}/segments", response_model=ReferenceSegmentsResponse)
def get_reference_segments(ref_id: str, db: Database = Depends(get_db)):
    return {"segments": db.get_segments_by_reference(ref_id)}
