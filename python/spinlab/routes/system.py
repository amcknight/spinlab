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
from spinlab.models import Mode, Status
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


@router.post("/cold-fill/start", response_model=OkResponse)
async def start_cold_fill(session: SessionManager = Depends(get_session)):
    """Start the cold-fill capture loop. SessionManager owns the transition;
    the dashboard's ActionError handler maps WrongModeError/NoGameLoadedError
    to 409 and NotConnectedError to 503."""
    result = await session.start_cold_fill()
    return {"status": "ok" if result.status != Status.NO_GAPS else "no_gaps"}


@router.post("/cold-fill/skip", response_model=OkResponse)
async def skip_cold_fill(session: SessionManager = Depends(get_session)):
    if session.mode != Mode.COLD_FILL:
        raise HTTPException(status_code=409, detail=f"Not in cold fill: mode is {session.mode.value}")
    result = await session.skip_cold_fill()
    return {"status": result.status.value}


@router.post("/cold-fill/abort", response_model=OkResponse)
async def abort_cold_fill(session: SessionManager = Depends(get_session)):
    if session.mode != Mode.COLD_FILL:
        raise HTTPException(status_code=409, detail=f"Not in cold fill: mode is {session.mode.value}")
    result = await session.abort_cold_fill()
    return {"status": result.status.value}


@router.post("/reset", response_model=OkResponse)
async def reset_data(session: SessionManager = Depends(get_session)):
    """Reset all data for the current game. SessionManager owns the sequence."""
    await session.reset_data()
    return {"status": "ok"}


_SNES_EXTS = {".sfc", ".smc", ".fig", ".swc"}
_RECENTLY_PLAYED_LIMIT = 3
_RECENTLY_ADDED_LIMIT = 2


@router.get("/roms", response_model=RomsResponse)
def list_roms(
    config: AppConfig = Depends(get_config),
    db: Database = Depends(get_db),
):
    rom_dir = config.rom_dir
    if not rom_dir or not rom_dir.is_dir():
        label = str(rom_dir) if rom_dir else ""
        return {"roms": [], "error": f"ROM directory not found: {label}"}

    rom_paths = [p for p in rom_dir.iterdir() if p.suffix.lower() in _SNES_EXTS]
    roms = sorted([p.name for p in rom_paths], key=str.lower)

    # Map stem (no extension, lowercase) → actual filename for matching.
    stem_to_rom = {p.stem.lower(): p.name for p in rom_paths}

    # Recently played: game names from DB → resolved to matching ROM filenames.
    played_names = db.get_recently_played_games(limit=_RECENTLY_PLAYED_LIMIT)
    recently_played = [
        stem_to_rom[name.lower()]
        for name in played_names
        if name.lower() in stem_to_rom
    ]

    # Recently added: ROM files sorted by creation time (Windows st_ctime),
    # excluding any already in recently_played so the two lists don't overlap.
    played_set = set(recently_played)
    by_ctime = sorted(rom_paths, key=lambda p: p.stat().st_ctime, reverse=True)
    recently_added = [
        p.name for p in by_ctime
        if p.name not in played_set
    ][:_RECENTLY_ADDED_LIMIT]

    return {
        "roms": roms,
        "recently_played": recently_played,
        "recently_added": recently_added,
    }


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
