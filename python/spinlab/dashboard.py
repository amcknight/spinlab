"""SpinLab dashboard — FastAPI web app, session manager, TCP client."""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .db import Database
from .session_manager import SessionManager
from .tcp_manager import TcpManager

logger = logging.getLogger(__name__)


def create_app(
    db: Database,
    rom_dir: Path | None = None,
    host: str = "127.0.0.1",
    port: int = 15482,
    config: dict | None = None,
    default_category: str = "any%",
) -> FastAPI:

    tcp = TcpManager(host, port)
    session = SessionManager(db, tcp, rom_dir, default_category)
    tcp.on_disconnect = session.on_disconnect

    async def _event_loop(session: SessionManager, tcp: TcpManager):
        while True:
            if not tcp.is_connected:
                await tcp.connect(timeout=2)
                if not tcp.is_connected:
                    await asyncio.sleep(2)
                    continue
            try:
                event = await tcp.recv_event(timeout=1.0)
                if event:
                    await session.route_event(event)
            except Exception:
                logger.exception("Error in event loop")
                await asyncio.sleep(1)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(_event_loop(session, tcp))
        yield
        task.cancel()
        await session.shutdown()

    app = FastAPI(title="SpinLab Dashboard", lifespan=lifespan)
    app.state.config = config or {}
    app.state.tcp = tcp
    app.state.session = session

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

    # -- Endpoints --

    @app.get("/")
    def root():
        return FileResponse(str(static_dir / "index.html"))

    @app.get("/api/state")
    def api_state():
        return session.get_state()

    @app.post("/api/reference/start")
    async def reference_start():
        return await session.start_reference()

    @app.post("/api/reference/stop")
    async def reference_stop():
        return await session.stop_reference()

    @app.post("/api/practice/start")
    async def practice_start():
        return await session.start_practice()

    @app.post("/api/practice/stop")
    async def practice_stop():
        return await session.stop_practice()

    # -- Model / allocator / estimator --

    @app.get("/api/model")
    def api_model():
        sched = session._get_scheduler()
        segments = sched.get_all_model_states()
        return {
            "estimator": sched.estimator.name,
            "allocator": sched.allocator.name,
            "segments": [
                {
                    "segment_id": s.segment_id,
                    "description": s.description,
                    "level_number": s.level_number,
                    "start_type": s.start_type,
                    "end_type": s.end_type,
                    "mu": round(s.estimator_state.mu, 2) if s.estimator_state else None,
                    "drift": round(s.estimator_state.d, 3) if s.estimator_state else None,
                    "marginal_return": round(s.marginal_return, 4),
                    "drift_info": s.drift_info,
                    "n_completed": s.n_completed,
                    "n_attempts": s.n_attempts,
                    "gold_ms": s.gold_ms,
                }
                for s in segments
            ],
        }

    @app.post("/api/allocator")
    def switch_allocator(body: dict):
        name = body.get("name")
        sched = session._get_scheduler()
        sched.switch_allocator(name)
        return {"allocator": name}

    @app.post("/api/estimator")
    def switch_estimator(body: dict):
        name = body.get("name")
        sched = session._get_scheduler()
        sched.switch_estimator(name)
        return {"estimator": name}

    @app.post("/api/reset")
    async def reset_data():
        await session.stop_practice()
        if session.mode == "reference":
            session._clear_ref_state()
        gid = session.game_id
        if gid:
            db.reset_game_data(gid)
        session.scheduler = None
        session.mode = "idle"
        return {"status": "ok"}

    @app.get("/api/segments")
    def api_segments():
        segments = db.get_all_segments_with_model(session._require_game())
        return {"segments": segments}

    @app.get("/api/sessions")
    def api_sessions():
        sessions = db.get_session_history(session._require_game())
        return {"sessions": sessions}

    # -- Reference management --

    @app.get("/api/references")
    def list_references():
        return {"references": db.list_capture_runs(session._require_game())}

    @app.post("/api/references")
    def create_reference(body: dict):
        import uuid
        run_id = f"ref_{uuid.uuid4().hex[:8]}"
        name = body.get("name", "Untitled")
        db.create_capture_run(run_id, session._require_game(), name)
        return {"id": run_id, "name": name}

    @app.patch("/api/references/{ref_id}")
    def rename_reference(ref_id: str, body: dict):
        name = body.get("name")
        if name:
            db.rename_capture_run(ref_id, name)
        return {"status": "ok"}

    @app.delete("/api/references/{ref_id}")
    def delete_reference(ref_id: str):
        db.delete_capture_run(ref_id)
        return {"status": "ok"}

    @app.post("/api/references/{ref_id}/activate")
    def activate_reference(ref_id: str):
        db.set_active_capture_run(ref_id)
        return {"status": "ok"}

    @app.get("/api/references/{ref_id}/segments")
    def get_reference_segments(ref_id: str):
        return {"segments": db.get_segments_by_reference(ref_id)}

    # -- Segment editing --

    @app.patch("/api/segments/{segment_id}")
    def update_segment_endpoint(segment_id: str, body: dict):
        db.update_segment(segment_id, **body)
        return {"status": "ok"}

    @app.delete("/api/segments/{segment_id}")
    def delete_segment(segment_id: str):
        db.soft_delete_segment(segment_id)
        return {"status": "ok"}

    @app.post("/api/segments/{segment_id}/fill-gap")
    async def fill_gap(segment_id: str):
        return await session.start_fill_gap(segment_id)

    # -- ROM listing --

    @app.get("/api/roms")
    def list_roms():
        cfg = app.state.config
        rom_dir = cfg.get("rom", {}).get("dir", "")
        if not rom_dir or not Path(rom_dir).is_dir():
            return {"roms": [], "error": f"ROM directory not found: {rom_dir}"}
        exts = {".sfc", ".smc", ".fig", ".swc"}
        roms = sorted(
            [p.name for p in Path(rom_dir).iterdir() if p.suffix.lower() in exts],
            key=str.lower,
        )
        return {"roms": roms}

    # -- Emulator launch --

    @app.post("/api/emulator/launch")
    def launch_emulator(body: dict | None = None):
        import subprocess
        cfg = app.state.config
        emu_path = cfg.get("emulator", {}).get("path", "")
        if not emu_path or not Path(emu_path).exists():
            return {"status": "error", "message": f"Emulator not found: {emu_path}"}

        # ROM: from request body, or fall back to config
        rom_dir = cfg.get("rom", {}).get("dir", "")
        rom_name = (body or {}).get("rom", "")
        if rom_name and rom_dir:
            rom_path = Path(rom_dir) / rom_name
        else:
            rom_path = Path(cfg.get("rom", {}).get("path", ""))
        if not rom_path.is_file():
            return {"status": "error", "message": f"ROM not found: {rom_path}"}

        lua_script = cfg.get("emulator", {}).get("lua_script", "")
        cmd = [emu_path, str(rom_path)]
        if lua_script:
            script_path = Path(lua_script)
            if not script_path.is_absolute():
                script_path = Path.cwd() / script_path
            if script_path.exists():
                cmd.append(str(script_path))
        subprocess.Popen(cmd)
        return {"status": "ok"}

    # -- Manifest import --

    @app.post("/api/import-manifest")
    def import_manifest(body: dict):
        import yaml
        from spinlab.manifest import seed_db_from_manifest
        manifest_path = Path(body["path"])
        with manifest_path.open(encoding="utf-8") as f:
            manifest = yaml.safe_load(f)
        game_name = manifest.get("game_id", session.game_id or "unknown")
        seed_db_from_manifest(db, manifest, game_name)
        return {"status": "ok", "segments_imported": len(manifest.get("segments", manifest.get("splits", [])))}

    # -- SSE --

    @app.get("/api/events")
    async def sse_events():
        from starlette.responses import StreamingResponse
        queue = session.subscribe_sse()
        async def event_stream():
            try:
                while True:
                    try:
                        state = await asyncio.wait_for(queue.get(), timeout=30)
                        yield f"data: {json.dumps(state)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            except asyncio.CancelledError:
                pass
            finally:
                session.unsubscribe_sse(queue)
        return StreamingResponse(event_stream(), media_type="text/event-stream")

    # -- Shutdown --

    @app.post("/api/shutdown")
    async def api_shutdown():
        await session.shutdown()
        import signal
        try:
            signal.raise_signal(signal.SIGINT)
        except (OSError, AttributeError):
            pass
        return {"status": "shutting_down"}

    return app
