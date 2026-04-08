"""SpinLab dashboard — FastAPI web app, session manager, TCP client."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

from .config import AppConfig, EmulatorConfig, NetworkConfig
from .db import Database
from .models import ActionResult, Status
from .session_manager import SessionManager
from .tcp_manager import TcpManager

logger = logging.getLogger(__name__)

TCP_CONNECT_TIMEOUT_S = 2
TCP_RETRY_DELAY_S = 2
TCP_EVENT_TIMEOUT_S = 1.0
SSE_KEEPALIVE_S = 30

_ERROR_STATUS_CODES: dict[Status, int] = {
    Status.NOT_CONNECTED: 503,
    Status.DRAFT_PENDING: 409,
    Status.PRACTICE_ACTIVE: 409,
    Status.REFERENCE_ACTIVE: 409,
    Status.ALREADY_RUNNING: 409,
    Status.ALREADY_REPLAYING: 409,
    Status.NOT_IN_REFERENCE: 409,
    Status.NOT_REPLAYING: 409,
    Status.NOT_RUNNING: 409,
    Status.NO_DRAFT: 404,
    Status.NO_HOT_VARIANT: 404,
}


def _check_result(result: ActionResult) -> dict:
    code = _ERROR_STATUS_CODES.get(result.status)
    if code:
        raise HTTPException(status_code=code, detail=result.status.value)
    return result.to_response()


async def event_loop(session: SessionManager, tcp: TcpManager) -> None:
    """Bridge TCP events to SessionManager. Extracted for testability."""
    while True:
        if not tcp.is_connected:
            await tcp.connect(timeout=TCP_CONNECT_TIMEOUT_S)
            if not tcp.is_connected:
                await asyncio.sleep(TCP_RETRY_DELAY_S)
                continue
        try:
            event = await tcp.recv_event(timeout=TCP_EVENT_TIMEOUT_S)
            if event:
                await session.route_event(event)
        except Exception:
            logger.exception("Error in event loop")
            await asyncio.sleep(1)


def create_app(
    db: Database,
    config: AppConfig | None = None,
) -> FastAPI:

    if config is None:
        config = AppConfig(
            network=NetworkConfig(),
            emulator=EmulatorConfig(),
            data_dir=Path("data"),
            rom_dir=None,
        )

    tcp = TcpManager(config.network.host, config.network.port)
    session = SessionManager(
        db, tcp, config.rom_dir, config.category, data_dir=config.data_dir,
        invalidate_combo=list(config.practice.invalidate_combo),
    )
    tcp.on_disconnect = session.on_disconnect

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(event_loop(session, tcp))
        yield
        task.cancel()
        await session.shutdown()

    app = FastAPI(title="SpinLab Dashboard", lifespan=lifespan)
    app.state.config = config
    app.state.tcp = tcp
    app.state.session = session
    app.state.db = db

    static_dir = Path(__file__).parent / "static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    from fastapi.responses import FileResponse
    from starlette.middleware.base import BaseHTTPMiddleware

    class NoCacheStaticMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            response = await call_next(request)
            if request.url.path.startswith("/static/"):
                response.headers["Cache-Control"] = "no-cache, must-revalidate"
            return response

    app.add_middleware(NoCacheStaticMiddleware)

    # -- Routers --

    from .routes.practice import router as practice_router
    from .routes.reference import router as reference_router
    from .routes.model import router as model_router
    from .routes.segments import router as segments_router
    from .routes.system import router as system_router
    from .routes.attempts import router as attempts_router
    from .routes.speed_run import router as speed_run_router

    app.include_router(practice_router)
    app.include_router(reference_router)
    app.include_router(model_router)
    app.include_router(segments_router)
    app.include_router(system_router)
    app.include_router(attempts_router)
    app.include_router(speed_run_router)

    # -- Root endpoint --

    index_path = static_dir / "index.html"

    @app.get("/")
    def root():
        if not index_path.exists():
            raise HTTPException(status_code=503, detail="Frontend not built. Run: cd frontend && npm run build")
        return FileResponse(str(index_path))

    return app
