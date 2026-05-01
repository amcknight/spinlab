"""Reference CRUD, drafts, and replay routes."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from spinlab.db import Database
from spinlab.protocol import SPEED_UNCAPPED
from spinlab.session_manager import SessionManager

from ._deps import get_db, get_session

router = APIRouter(prefix="/api")


@router.post("/reference/start")
async def reference_start(session: SessionManager = Depends(get_session)):
    return (await session.start_reference()).to_response()


@router.post("/reference/stop")
async def reference_stop(session: SessionManager = Depends(get_session)):
    return (await session.stop_reference()).to_response()


@router.post("/replay/start")
async def replay_start(req: Request, session: SessionManager = Depends(get_session)):
    body = await req.json()
    ref_id = body.get("ref_id")
    speed = body.get("speed", SPEED_UNCAPPED)
    if not ref_id:
        raise HTTPException(status_code=400, detail="ref_id required")
    gid = session.game_id or "unknown"
    spinrec_path = str((session.data_dir / gid / "rec" / f"{ref_id}.spinrec").resolve())
    return (await session.start_replay(spinrec_path, speed=speed)).to_response()


@router.post("/replay/stop")
async def replay_stop(session: SessionManager = Depends(get_session)):
    return (await session.stop_replay()).to_response()


@router.get("/references")
def list_references(session: SessionManager = Depends(get_session), db: Database = Depends(get_db)):
    if session.game_id is None:
        return {"references": []}
    gid = session.game_id
    refs = db.list_capture_runs(gid)
    out: list[dict] = []
    for ref in refs:
        d: dict = dict(ref)
        rec_path = session.data_dir / gid / "rec" / f"{d['id']}.spinrec"
        d["has_spinrec"] = rec_path.is_file()
        out.append(d)
    return {"references": out}


@router.post("/references")
def create_reference(body: dict, session: SessionManager = Depends(get_session), db: Database = Depends(get_db)):
    import uuid
    run_id = f"ref_{uuid.uuid4().hex[:8]}"
    name = body.get("name", "Untitled")
    db.create_capture_run(run_id, session.require_game(), name)
    return {"id": run_id, "name": name}


@router.post("/reference/finalize")
async def reference_finalize(req: Request, session: SessionManager = Depends(get_session)):
    body = await req.json()
    name = body.get("name", "Untitled")
    return (await session.finalize_run(name)).to_response()


@router.post("/reference/save_and_finish")
async def reference_save_and_finish(req: Request, session: SessionManager = Depends(get_session)):
    body = await req.json()
    name = body.get("name", "Untitled")
    return (await session.save_and_finish_run(name)).to_response()


@router.post("/reference/discard_run")
async def reference_discard_run(session: SessionManager = Depends(get_session)):
    return (await session.discard_run()).to_response()


@router.post("/reference/resume")
async def reference_resume(session: SessionManager = Depends(get_session)):
    return (await session.resume_reference()).to_response()


@router.delete("/capture_sessions/{session_id}")
async def delete_capture_session(session_id: str, session: SessionManager = Depends(get_session)):
    return (await session.delete_capture_session(session_id)).to_response()


@router.get("/capture_sessions")
def list_capture_sessions(
    run_id: str,
    session: SessionManager = Depends(get_session),
    db: Database = Depends(get_db),
):
    return {"sessions": db.list_capture_sessions_for_run(run_id)}


@router.get("/references/{ref_id}/spinrec")
def check_spinrec(ref_id: str, session: SessionManager = Depends(get_session)):
    gid = session.game_id or "unknown"
    rec_path = session.data_dir / gid / "rec" / f"{ref_id}.spinrec"
    if rec_path.is_file():
        return {"exists": True, "path": str(rec_path)}
    return {"exists": False}


@router.patch("/references/{ref_id}")
def rename_reference(ref_id: str, body: dict, db: Database = Depends(get_db)):
    name = body.get("name")
    if name:
        db.rename_capture_run(ref_id, name)
    return {"status": "ok"}


@router.delete("/references/{ref_id}")
def delete_reference(ref_id: str, db: Database = Depends(get_db)):
    db.delete_capture_run(ref_id)
    return {"status": "ok"}


@router.post("/references/{ref_id}/activate")
def activate_reference(ref_id: str, db: Database = Depends(get_db)):
    db.set_active_capture_run(ref_id)
    return {"status": "ok"}


@router.get("/references/{ref_id}/segments")
def get_reference_segments(ref_id: str, db: Database = Depends(get_db)):
    return {"segments": db.get_segments_by_reference(ref_id)}
