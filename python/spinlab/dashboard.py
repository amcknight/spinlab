"""SpinLab dashboard — FastAPI web app for live stats and management."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .db import Database


def _read_state_file(path: Path) -> Optional[dict]:
    """Read orchestrator state file, returning None if missing/invalid."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def create_app(
    db: Database,
    game_id: str,
    state_file: Path,
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

    # Lazy-init scheduler for API calls that need it
    _scheduler = None

    def _get_scheduler():
        nonlocal _scheduler
        if _scheduler is None:
            _scheduler = Scheduler(db, game_id)
        return _scheduler

    @app.get("/")
    def root():
        return FileResponse(str(static_dir / "index.html"))

    @app.get("/api/state")
    def api_state():
        orch_state = _read_state_file(state_file)
        session = db.get_current_session(game_id)

        if not session:
            return {
                "mode": "idle",
                "current_split": None,
                "queue": [],
                "recent": [],
                "session": None,
                "allocator": _get_scheduler().allocator.name,
            }

        # Validate state file matches active session (if session_id present)
        state_sid = orch_state.get("session_id") if orch_state else None
        if state_sid and state_sid != session["id"]:
            orch_state = None  # stale state file from old session

        mode = "practice" if orch_state else "reference"

        current_split = None
        queue: list[dict] = []
        if orch_state:
            split_id = orch_state.get("current_split_id")
            if split_id:
                splits = db.get_all_splits_with_model(game_id)
                split_map = {s["id"]: s for s in splits}
                if split_id in split_map:
                    current_split = split_map[split_id]
                    current_split["attempt_count"] = db.get_split_attempt_count(
                        split_id, session["id"]
                    )
                    # Add drift info from model state
                    model_row = db.load_model_state(split_id)
                    if model_row and model_row["state_json"]:
                        from spinlab.estimators.kalman import KalmanState
                        from spinlab.estimators import get_estimator
                        state = KalmanState.from_dict(json.loads(model_row["state_json"]))
                        est = get_estimator(model_row["estimator"])
                        current_split["drift_info"] = est.drift_info(state)

            queue_ids: list[str] = orch_state.get("queue", [])
            if queue_ids:
                splits = db.get_all_splits_with_model(game_id)
                split_map = {s["id"]: s for s in splits}
                queue = [split_map[sid] for sid in queue_ids if sid in split_map]

            # Pass allocator/estimator from state file
            if orch_state.get("allocator"):
                pass  # returned in response below

        recent = db.get_recent_attempts(game_id, limit=8)

        sched = _get_scheduler()
        return {
            "mode": mode,
            "current_split": current_split,
            "queue": queue,
            "recent": recent,
            "session": dict(session),
            "allocator": sched.allocator.name,
            "estimator": sched.estimator.name,
        }

    @app.get("/api/model")
    def api_model():
        """All splits with full estimator state for Model tab."""
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
        """Clear all session data (attempts, sessions, model state)."""
        nonlocal _scheduler
        db.reset_all_data()
        _scheduler = None  # force re-init with fresh defaults
        # Remove stale state file
        if state_file.exists():
            state_file.unlink()
        return {"status": "ok"}

    @app.get("/api/splits")
    def api_splits():
        splits = db.get_all_splits_with_model(game_id)
        return {"splits": splits}

    @app.get("/api/sessions")
    def api_sessions():
        sessions = db.get_session_history(game_id)
        return {"sessions": sessions}

    return app
