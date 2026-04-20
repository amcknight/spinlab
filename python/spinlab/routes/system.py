"""System routes: state, SSE, sessions, ROMs, emulator, reset, shutdown."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Depends, HTTPException, Request

from spinlab.config import AppConfig
from spinlab.dashboard import SSE_KEEPALIVE_S
from spinlab.db import Database
from spinlab.models import Mode
from spinlab.session_manager import SessionManager

from ._deps import get_config, get_db, get_session

router = APIRouter(prefix="/api")


@router.get("/state")
def api_state(session: SessionManager = Depends(get_session)):
    return session.get_state()


@router.get("/events")
async def sse_events(session: SessionManager = Depends(get_session)):
    from starlette.responses import StreamingResponse
    queue = session.subscribe_sse()

    async def event_stream():
        try:
            while True:
                try:
                    state = await asyncio.wait_for(queue.get(), timeout=SSE_KEEPALIVE_S)
                    yield f"data: {json.dumps(state)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            session.unsubscribe_sse(queue)
    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.get("/sessions")
def api_sessions(session: SessionManager = Depends(get_session), db: Database = Depends(get_db)):
    if session.game_id is None:
        return {"sessions": []}
    sessions = db.get_session_history(session.game_id)
    return {"sessions": sessions}


@router.post("/reset")
async def reset_data(session: SessionManager = Depends(get_session), db: Database = Depends(get_db)):
    from spinlab.errors import NotRunningError
    try:
        await session.stop_practice()
    except NotRunningError:
        pass
    if session.mode == Mode.REFERENCE:
        session._clear_ref_and_idle()
    gid = session.game_id
    if gid:
        logger.warning("reset: clearing all data for game=%s", gid)
        db.reset_game_data(gid)
    session.scheduler = None
    session.mode = Mode.IDLE
    return {"status": "ok"}


@router.get("/roms")
def list_roms(config: AppConfig = Depends(get_config)):
    rom_dir = config.rom_dir
    if not rom_dir or not rom_dir.is_dir():
        label = str(rom_dir) if rom_dir else ""
        return {"roms": [], "error": f"ROM directory not found: {label}"}
    exts = {".sfc", ".smc", ".fig", ".swc"}
    roms = sorted(
        [p.name for p in rom_dir.iterdir() if p.suffix.lower() in exts],
        key=str.lower,
    )
    return {"roms": roms}


@router.post("/emulator/launch")
def launch_emulator(body: dict | None = None, config: AppConfig = Depends(get_config)):
    import subprocess
    emu_path = config.emulator.path
    if not emu_path or not emu_path.exists():
        raise HTTPException(status_code=400, detail=f"Emulator not found: {emu_path}")

    # ROM: from request body, or fall back to config
    rom_dir = config.rom_dir
    rom_name = (body or {}).get("rom", "")
    if rom_name and rom_dir:
        rom_path = rom_dir / rom_name
    else:
        rom_path = Path("")

    if rom_dir:
        resolved_rom = rom_path.resolve()
        resolved_dir = rom_dir.resolve()
        if not str(resolved_rom).startswith(str(resolved_dir)):
            raise HTTPException(status_code=400, detail="ROM path outside rom_dir")

    if not rom_path.is_file():
        raise HTTPException(status_code=400, detail=f"ROM not found: {rom_path}")

    lua_script = config.emulator.lua_script
    cmd = [str(emu_path), str(rom_path)]
    if lua_script:
        script_path = lua_script if lua_script.is_absolute() else Path.cwd() / lua_script
        if script_path.exists():
            cmd.append(str(script_path))
            # Write breadcrumb so Lua can find addresses.lua even when Mesen auto-loads
            script_data_dir = config.emulator.script_data_dir
            if script_data_dir:
                lua_dir = str(script_path.resolve().parent) + "/"
                breadcrumb = Path(script_data_dir) / "lua_dir.txt"
                breadcrumb.parent.mkdir(parents=True, exist_ok=True)
                breadcrumb.write_text(lua_dir, encoding="utf-8")
    subprocess.Popen(cmd)
    return {"status": "ok"}


@router.post("/shutdown")
async def api_shutdown(session: SessionManager = Depends(get_session)):
    await session.shutdown()
    import signal
    try:
        signal.raise_signal(signal.SIGINT)
    except (OSError, AttributeError):
        pass
    return {"status": "shutting_down"}
