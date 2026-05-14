"""System routes: state, SSE, sessions, ROMs, emulator, reset, shutdown."""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Depends, HTTPException

from spinlab.api_schemas import (
    AppState,
    EmulatorLaunchRequest,
    EmulatorLaunchResponse,
    OkResponse,
    RomsResponse,
    SessionsResponse,
    ShutdownResponse,
)
from spinlab.config import AppConfig
from spinlab.dashboard import SSE_KEEPALIVE_S
from spinlab.db import Database
from spinlab.models import Mode
from spinlab.session_manager import SessionManager

from ._deps import get_config, get_db, get_session

router = APIRouter(prefix="/api")


@router.get("/state", response_model=AppState)
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


@router.get("/sessions", response_model=SessionsResponse)
def api_sessions(session: SessionManager = Depends(get_session), db: Database = Depends(get_db)):
    if session.game_id is None:
        return {"sessions": []}
    sessions = db.get_session_history(session.game_id)
    return {"sessions": sessions}


@router.post("/reset", response_model=OkResponse)
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


@router.get("/roms", response_model=RomsResponse)
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


def _retroarch_already_running(nci_port: int) -> bool:
    """Probe NCI to detect if RetroArch is already up. Short timeout — best effort."""
    try:
        from spinlab.retroarch.exceptions import NCIError
        from spinlab.retroarch.nci import NCIClient
    except ImportError:
        return False
    client = NCIClient(port=nci_port, timeout=0.3)
    try:
        client.version()
        return True
    except NCIError:
        return False
    finally:
        client.close()


def _launch_emulator(body: EmulatorLaunchRequest | None, config: AppConfig) -> dict:
    """Launch RetroArch with the requested ROM, or no-op if RA's already running.

    Click a game in the dashboard → emulator pops up with that ROM loaded.
    If RA is already running we don't launch a second instance (it would fight
    over the NCI port); the user can switch ROMs from inside RA's Quick Menu.
    """
    emu = config.emulator
    if _retroarch_already_running(config.network.nci_port):
        return {"status": "already_running"}

    if not emu.retroarch_path or not Path(emu.retroarch_path).exists():
        raise HTTPException(
            status_code=400,
            detail=(
                f"emulator.retroarch_path not configured or missing: "
                f"{emu.retroarch_path}. See "
                f"docs/retroarch-migration/launch-retroarch.md."
            ),
        )

    rom_dir = config.rom_dir
    rom_name = body.rom if body else ""
    if not rom_name:
        raise HTTPException(status_code=400, detail="No ROM specified in request body")
    if not rom_dir:
        raise HTTPException(status_code=400, detail="rom.dir not configured")
    rom_path = (rom_dir / rom_name).resolve()
    if not str(rom_path).startswith(str(rom_dir.resolve())):
        raise HTTPException(status_code=400, detail="ROM path outside rom_dir")
    if not rom_path.is_file():
        raise HTTPException(status_code=400, detail=f"ROM not found: {rom_path}")

    # Default core path: <retroarch_dir>/cores/snes9x_libretro.dll. If it
    # doesn't exist, omit -L and let RA auto-pick (which works if the user
    # has loaded SNES content with snes9x_libretro before).
    core_path = Path(emu.retroarch_path).parent / "cores" / "snes9x_libretro.dll"
    cmd = [str(emu.retroarch_path)]
    if core_path.exists():
        cmd += ["-L", str(core_path)]
    cmd.append(str(rom_path))
    logger.info("Launching RetroArch: %s", cmd)
    subprocess.Popen(cmd)
    return {"status": "ok"}


@router.post("/emulator/launch", response_model=EmulatorLaunchResponse)
def launch_emulator(body: EmulatorLaunchRequest | None = None, config: AppConfig = Depends(get_config)):
    return _launch_emulator(body, config)


@router.post("/shutdown", response_model=ShutdownResponse)
async def api_shutdown(session: SessionManager = Depends(get_session)):
    await session.shutdown()
    import signal
    try:
        signal.raise_signal(signal.SIGINT)
    except (OSError, AttributeError):
        pass
    return {"status": "shutting_down"}
