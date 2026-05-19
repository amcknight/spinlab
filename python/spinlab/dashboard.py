"""SpinLab dashboard — FastAPI web app, session manager, emulator bridge."""
from __future__ import annotations

import asyncio
import logging
import subprocess
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .config import AppConfig, EmulatorConfig, NetworkConfig
from .db import Database
from .emu_backend import EmuBackend
from .errors import ActionError
from .session_manager import SessionManager

logger = logging.getLogger(__name__)

EMU_CONNECT_TIMEOUT_S = 2
EMU_RETRY_DELAY_S = 2
EMU_EVENT_TIMEOUT_S = 1.0
SSE_KEEPALIVE_S = 30


def _prewarm_segments_model() -> None:
    """JIT-compile the JAX kernels for the v07 segments model.

    Without prewarm, the first refit per process pays a ~200-400ms JIT
    cost per attempt-bucket size; with prewarm, ~15ms steady-state.
    Total prewarm cost is ~10s; we eat it in a daemon thread (see
    ``_run_startup_hooks``) so the dashboard responds before it
    finishes.

    Catches ImportError specifically so a dev environment without the
    ``[fits]`` extra (jax/numpyro/jaxopt) boots cleanly — only the
    silent fit pipeline is silently disabled. Other exceptions are
    logged at exception level and swallowed so a prewarm bug never
    kills the thread (or the dashboard process).
    """
    try:
        from spinlab.segments_model import prewarm_buckets
    except ImportError:
        logger.info(
            "segments_model not installed ([fits] extra); skipping JAX prewarm."
        )
        return
    logger.info("segments-v07 JAX prewarm started in background thread")
    try:
        prewarm_buckets()
    except Exception:
        logger.exception("segments-v07 JAX prewarm failed")
        return
    logger.info("segments-v07 JAX prewarm complete")


def _prewarm_thread_target() -> None:
    """Thread entry point: invoke _prewarm_segments_model with defense-in-depth.

    ``_prewarm_segments_model`` already catches ImportError and logs
    other exceptions, but tests / future refactors may swap that
    function out. Belt-and-suspenders: never let an exception escape
    the thread (uncaught thread exceptions become noisy warnings in
    tests and unhandled errors in production).
    """
    try:
        _prewarm_segments_model()
    except Exception:
        logger.exception("segments-v07 prewarm thread crashed")


def _run_startup_hooks() -> None:
    """Single sync entry point for dashboard startup-time background work.

    Extracted so unit tests can drive the hook without spinning up a
    FastAPI app + event loop. Production wires this into the lifespan
    context manager below.
    """
    threading.Thread(
        target=_prewarm_thread_target,
        name="segments-v07-prewarm",
        daemon=True,
    ).start()


async def event_loop(session: SessionManager, emu: EmuBackend) -> None:
    """Bridge backend events to SessionManager. Extracted for testability."""
    while True:
        if not emu.is_connected:
            await emu.connect(timeout=EMU_CONNECT_TIMEOUT_S)
            if not emu.is_connected:
                await asyncio.sleep(EMU_RETRY_DELAY_S)
                continue
        try:
            event = await emu.recv_event(timeout=EMU_EVENT_TIMEOUT_S)
            if event:
                await session.route_event(event)
        except Exception:
            logger.exception("Error in event loop")
            await asyncio.sleep(1)


def create_app(
    db: Database,
    config: AppConfig | None = None,
    vite_process: subprocess.Popen | None = None,
) -> FastAPI:

    if config is None:
        config = AppConfig(
            network=NetworkConfig(),
            emulator=EmulatorConfig(),
            data_dir=Path("data"),
            rom_dir=None,
        )

    from spinlab.retroarch.wiring import build_orchestrator
    emu: EmuBackend = build_orchestrator(config)
    session = SessionManager(
        db, emu, config.rom_dir, config.category,
        data_dir=config.data_dir,
    )
    emu.on_disconnect = session.on_disconnect

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        _run_startup_hooks()
        task = asyncio.create_task(event_loop(session, emu))
        yield
        task.cancel()
        await session.shutdown()
        if vite_process is not None:
            from .vite import terminate_vite
            terminate_vite(vite_process)

    app = FastAPI(title="SpinLab Dashboard", lifespan=lifespan)

    @app.exception_handler(ActionError)
    async def action_error_handler(request: Request, exc: ActionError):
        return JSONResponse(status_code=exc.http_code, content={"detail": exc.detail})

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

    app.state.config = config
    app.state.emu = emu
    app.state.session = session
    app.state.db = db

    from .routes.attempts import router as attempts_router
    from .routes.model import router as model_router
    from .routes.practice import router as practice_router
    from .routes.reference import router as reference_router
    from .routes.segments import router as segments_router
    from .routes.speed_run import router as speed_run_router
    from .routes.system import router as system_router

    app.include_router(practice_router)
    app.include_router(reference_router)
    app.include_router(model_router)
    app.include_router(segments_router)
    app.include_router(system_router)
    app.include_router(attempts_router)
    app.include_router(speed_run_router)

    return app
