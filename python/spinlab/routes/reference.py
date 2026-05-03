"""Reference CRUD, drafts, and replay routes."""
from __future__ import annotations

from pathlib import Path

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


def _resolve_spinrec_path(
    ref_id: str,
    session: "SessionManager",
    db: Database,
    session_id: str | None = None,
) -> str | None:
    """Return the spinrec_path for a reference run.

    Lookup strategy:
    1. If session_id is given, use that session's spinrec_path directly.
    2. Otherwise use the first session's spinrec_path (ordinal 1).
    3. Fall back to the legacy ``{ref_id}.spinrec`` path for runs created before
       multi-session support (i.e. runs with no capture_sessions rows).

    Returns None if no path can be determined.
    """
    sessions = db.list_capture_sessions_for_run(ref_id)
    if sessions:
        if session_id:
            target = next((s for s in sessions if s["id"] == session_id), None)
            return target["spinrec_path"] if target else None
        return sessions[0]["spinrec_path"]
    # Legacy single-file layout: a run with no capture_sessions rows still has
    # a spinrec at the path named after the run id.
    gid = session.game_id or "unknown"
    legacy_path = session.data_dir / gid / "rec" / f"{ref_id}.spinrec"
    return str(legacy_path.resolve())


@router.post("/replay/start")
async def replay_start(req: Request, session: SessionManager = Depends(get_session), db: Database = Depends(get_db)):
    body = await req.json()
    ref_id = body.get("ref_id")
    session_id = body.get("session_id")  # optional: replay a specific session
    speed = body.get("speed", SPEED_UNCAPPED)
    if not ref_id:
        raise HTTPException(status_code=400, detail="ref_id required")
    spinrec_path = _resolve_spinrec_path(ref_id, session, db, session_id=session_id)
    if not spinrec_path:
        raise HTTPException(status_code=404, detail="spinrec_not_found")
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
        ref_id = d["id"]
        sessions = db.list_capture_sessions_for_run(ref_id)
        if sessions:
            # has_spinrec = True if any session's file is present on disk
            d["has_spinrec"] = any(
                Path(s["spinrec_path"]).is_file() for s in sessions
            )
        else:
            # Legacy single-file layout (no capture_sessions rows)
            legacy_path = session.data_dir / gid / "rec" / f"{ref_id}.spinrec"
            d["has_spinrec"] = legacy_path.is_file()
        out.append(d)
    return {"references": out}


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
def check_spinrec(ref_id: str, session: SessionManager = Depends(get_session), db: Database = Depends(get_db)):
    spinrec_path = _resolve_spinrec_path(ref_id, session, db)
    if spinrec_path and Path(spinrec_path).is_file():
        return {"exists": True, "path": spinrec_path}
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
