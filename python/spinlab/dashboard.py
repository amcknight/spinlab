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
    app = FastAPI(title="SpinLab Dashboard")

    static_dir = Path(__file__).parent / "static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    from fastapi.responses import FileResponse

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
            }

        mode = "practice" if orch_state else "reference"

        current_split = None
        queue = []
        if orch_state:
            split_id = orch_state.get("current_split_id")
            if split_id:
                row = db.get_split_with_schedule(split_id)
                if row:
                    row["attempt_count"] = db.get_split_attempt_count(
                        split_id, session["id"]
                    )
                    current_split = row

            queue = db.get_splits_summary_by_ids(
                orch_state.get("queue", [])
            )

        recent = db.get_recent_attempts(game_id, limit=8)

        return {
            "mode": mode,
            "current_split": current_split,
            "queue": queue,
            "recent": recent,
            "session": dict(session),
        }

    @app.get("/api/splits")
    def api_splits():
        splits = db.get_all_splits_with_schedule(game_id)
        return {"splits": splits}

    @app.get("/api/sessions")
    def api_sessions():
        sessions = db.get_session_history(game_id)
        return {"sessions": sessions}

    return app
