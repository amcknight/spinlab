"""SpinLab dashboard — FastAPI web app, session manager, TCP client."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .db import Database
from .tcp_manager import TcpManager
from .practice import PracticeSession

logger = logging.getLogger(__name__)


def create_app(
    db: Database,
    game_id: str,
    state_file: Path | None = None,  # deprecated, ignored
    host: str = "127.0.0.1",
    port: int = 15482,
) -> FastAPI:
    from spinlab.scheduler import Scheduler

    app = FastAPI(title="SpinLab Dashboard")

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

    # -- Shared state --
    tcp = TcpManager(host, port)
    _scheduler: list = [None]  # mutable container for nonlocal
    _practice: list = [None]   # PracticeSession | None
    _practice_task: list = [None]  # asyncio.Task | None
    _reconnect_task: list = [None]

    # Expose internal state for testing
    app.state.tcp = tcp
    app.state._practice = _practice
    app.state._scheduler = _scheduler

    def _get_scheduler() -> Scheduler:
        if _scheduler[0] is None:
            _scheduler[0] = Scheduler(db, game_id)
        return _scheduler[0]

    def _current_mode() -> str:
        if _practice[0] and _practice[0].is_running:
            return "practice"
        if tcp.is_connected:
            return "reference"
        return "idle"

    # -- Reference capture state --
    _ref_pending: dict[tuple, dict] = {}  # (level, room) -> entrance event
    _ref_splits_count: list[int] = [0]
    _ref_capture_run_id: list[str | None] = [None]

    def _clear_ref_state():
        """Clear reference capture state on disconnect or mode change."""
        _ref_pending.clear()
        _ref_splits_count[0] = 0
        _ref_capture_run_id[0] = None

    tcp.on_disconnect = _clear_ref_state

    # -- TCP auto-reconnect --
    async def _reconnect_loop():
        while True:
            await asyncio.sleep(3)
            if not tcp.is_connected:
                await tcp.connect(timeout=2)

    async def _event_dispatch_loop():
        """Single event consumer: dispatches to reference capture when not practicing."""
        while True:
            if not tcp.is_connected:
                await asyncio.sleep(1)
                continue
            try:
                event = await tcp.recv_event(timeout=1.0)
                if event is None:
                    continue

                # During practice, PracticeSession reads from the same queue
                if _practice[0] and _practice[0].is_running:
                    continue

                # Reference mode: pair transition events into splits
                evt_type = event.get("event")
                if evt_type == "level_entrance":
                    key = (event["level"], event["room"])
                    _ref_pending[key] = event

                    # Create capture_run on first entrance event
                    if _ref_capture_run_id[0] is None:
                        run_id = f"live_{uuid.uuid4().hex[:8]}"
                        run_name = f"Live {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}"
                        db.create_capture_run(run_id, game_id, run_name)
                        db.set_active_capture_run(run_id)
                        _ref_capture_run_id[0] = run_id

                elif evt_type == "level_exit":
                    key = (event["level"], event["room"])
                    goal = event.get("goal", "abort")
                    if goal == "abort":
                        _ref_pending.pop(key, None)
                        continue
                    entrance = _ref_pending.pop(key, None)
                    if entrance:
                        _ref_splits_count[0] += 1
                        from .models import Split
                        split_id = Split.make_id(
                            game_id, entrance["level"], entrance["room"], goal
                        )
                        split = Split(
                            id=split_id,
                            game_id=game_id,
                            level_number=entrance["level"],
                            room_id=entrance["room"],
                            goal=goal,
                            state_path=entrance.get("state_path"),
                            reference_time_ms=event.get("elapsed_ms"),
                            ordinal=_ref_splits_count[0],
                            reference_id=_ref_capture_run_id[0],
                        )
                        db.upsert_split(split)
            except Exception:
                await asyncio.sleep(1)

    @app.on_event("startup")
    async def startup():
        _reconnect_task[0] = asyncio.create_task(_reconnect_loop())
        asyncio.create_task(_event_dispatch_loop())

    @app.on_event("shutdown")
    async def shutdown():
        if _reconnect_task[0]:
            _reconnect_task[0].cancel()
        if _practice_task[0]:
            _practice_task[0].cancel()
        await tcp.disconnect()

    # -- Endpoints --

    @app.get("/")
    def root():
        return FileResponse(str(static_dir / "index.html"))

    @app.get("/api/state")
    def api_state():
        mode = _current_mode()
        sched = _get_scheduler()

        current_split = None
        queue: list[dict] = []
        session_dict = None

        if mode == "practice" and _practice[0]:
            ps = _practice[0]
            session_dict = {
                "id": ps.session_id,
                "started_at": ps.started_at,
                "splits_attempted": ps.splits_attempted,
                "splits_completed": ps.splits_completed,
            }
            if ps.current_split_id:
                splits = db.get_all_splits_with_model(game_id)
                split_map = {s["id"]: s for s in splits}
                if ps.current_split_id in split_map:
                    current_split = split_map[ps.current_split_id]
                    current_split["attempt_count"] = db.get_split_attempt_count(
                        ps.current_split_id, ps.session_id
                    )
                    model_row = db.load_model_state(ps.current_split_id)
                    if model_row and model_row["state_json"]:
                        from spinlab.estimators.kalman import KalmanState
                        from spinlab.estimators import get_estimator
                        state = KalmanState.from_dict(json.loads(model_row["state_json"]))
                        est = get_estimator(model_row["estimator"])
                        current_split["drift_info"] = est.drift_info(state)

            # Queue from scheduler
            queue_ids = sched.peek_next_n(3)
            if ps.current_split_id:
                queue_ids = [q for q in queue_ids if q != ps.current_split_id][:2]
            splits_all = db.get_all_splits_with_model(game_id)
            smap = {s["id"]: s for s in splits_all}
            queue = [smap[sid] for sid in queue_ids if sid in smap]

        recent = db.get_recent_attempts(game_id, limit=8)

        return {
            "mode": mode,
            "tcp_connected": tcp.is_connected,
            "current_split": current_split,
            "queue": queue,
            "recent": recent,
            "session": session_dict,
            "sections_captured": _ref_splits_count[0],
            "allocator": sched.allocator.name,
            "estimator": sched.estimator.name,
        }

    @app.post("/api/practice/start")
    async def practice_start():
        if _practice[0] and _practice[0].is_running:
            return {"status": "already_running"}
        if not tcp.is_connected:
            return {"status": "not_connected"}

        ps = PracticeSession(tcp=tcp, db=db, game_id=game_id)
        _practice[0] = ps
        _practice_task[0] = asyncio.create_task(ps.run_loop())
        return {"status": "started", "session_id": ps.session_id}

    @app.post("/api/practice/stop")
    async def practice_stop():
        if _practice[0] and _practice[0].is_running:
            _practice[0].is_running = False
            # Wait briefly for clean shutdown
            if _practice_task[0]:
                try:
                    await asyncio.wait_for(_practice_task[0], timeout=5)
                except asyncio.TimeoutError:
                    _practice_task[0].cancel()
            return {"status": "stopped"}
        return {"status": "not_running"}

    # -- Model / allocator / estimator --

    @app.get("/api/model")
    def api_model():
        sched = _get_scheduler()
        splits = sched.get_all_model_states()
        return {
            "estimator": sched.estimator.name,
            "allocator": sched.allocator.name,
            "splits": [
                {
                    "split_id": s.split_id,
                    "goal": s.goal,
                    "description": s.description,
                    "level_number": s.level_number,
                    "mu": round(s.estimator_state.mu, 2) if s.estimator_state else None,
                    "drift": round(s.estimator_state.d, 3) if s.estimator_state else None,
                    "marginal_return": round(s.marginal_return, 4),
                    "drift_info": s.drift_info,
                    "n_completed": s.n_completed,
                    "n_attempts": s.n_attempts,
                    "gold_ms": s.gold_ms,
                    "reference_time_ms": s.reference_time_ms,
                }
                for s in splits
            ],
        }

    @app.post("/api/allocator")
    def switch_allocator(body: dict):
        name = body.get("name")
        sched = _get_scheduler()
        sched.switch_allocator(name)
        return {"allocator": name}

    @app.post("/api/estimator")
    def switch_estimator(body: dict):
        name = body.get("name")
        sched = _get_scheduler()
        sched.switch_estimator(name)
        return {"estimator": name}

    @app.post("/api/reset")
    def reset_data():
        db.reset_all_data()
        _scheduler[0] = None  # force re-init with fresh defaults
        return {"status": "ok"}

    @app.get("/api/splits")
    def api_splits():
        splits = db.get_all_splits_with_model(game_id)
        return {"splits": splits}

    @app.get("/api/sessions")
    def api_sessions():
        sessions = db.get_session_history(game_id)
        return {"sessions": sessions}

    # -- Reference management --

    @app.get("/api/references")
    def list_references():
        return {"references": db.list_capture_runs(game_id)}

    @app.post("/api/references")
    def create_reference(body: dict):
        run_id = f"ref_{uuid.uuid4().hex[:8]}"
        name = body.get("name", "Untitled")
        db.create_capture_run(run_id, game_id, name)
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

    @app.get("/api/references/{ref_id}/splits")
    def get_reference_splits(ref_id: str):
        return {"splits": db.get_splits_by_reference(ref_id)}

    # -- Split editing --

    @app.patch("/api/splits/{split_id}")
    def update_split_endpoint(split_id: str, body: dict):
        db.update_split(split_id, **body)
        return {"status": "ok"}

    @app.delete("/api/splits/{split_id}")
    def delete_split(split_id: str):
        db.soft_delete_split(split_id)
        return {"status": "ok"}

    # -- Manifest import --

    @app.post("/api/import-manifest")
    def import_manifest(body: dict):
        import yaml
        from spinlab.manifest import seed_db_from_manifest
        manifest_path = Path(body["path"])
        with manifest_path.open(encoding="utf-8") as f:
            manifest = yaml.safe_load(f)
        game_name = manifest.get("game_id", game_id)
        seed_db_from_manifest(db, manifest, game_name)
        return {"status": "ok", "splits_imported": len(manifest.get("splits", []))}

    return app
