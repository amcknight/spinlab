"""SpinLab dashboard — FastAPI web app, session manager, TCP client."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime
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
    rom_dir: Path | None = None,
    host: str = "127.0.0.1",
    port: int = 15482,
    config: dict | None = None,
    default_category: str = "any%",
) -> FastAPI:
    from spinlab.scheduler import Scheduler

    app = FastAPI(title="SpinLab Dashboard")
    app.state.config = config or {}

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
    _mode: list[str] = ["idle"]  # "idle" | "reference" | "practice"
    _game_id: list[str | None] = [None]
    _game_name: list[str | None] = [None]

    # Expose internal state for testing
    app.state.tcp = tcp
    app.state._practice = _practice
    app.state._scheduler = _scheduler
    app.state._mode = _mode
    app.state._game_id = _game_id
    app.state._game_name = _game_name

    def _require_game() -> str:
        """Return current game_id or raise HTTPException."""
        gid = _game_id[0]
        if gid is None:
            from fastapi import HTTPException
            raise HTTPException(status_code=409, detail="No game loaded")
        return gid

    def _get_scheduler() -> Scheduler:
        if _scheduler[0] is None:
            _scheduler[0] = Scheduler(db, _require_game())
        return _scheduler[0]

    def _current_mode() -> str:
        return _mode[0]

    # -- Reference capture state --
    _ref_pending: dict[tuple, dict] = {}  # (level, room) -> entrance event
    _ref_splits_count: list[int] = [0]
    _ref_capture_run_id: list[str | None] = [None]

    def _clear_ref_state():
        """Clear reference capture state on disconnect or mode change."""
        _ref_pending.clear()
        _ref_splits_count[0] = 0
        _ref_capture_run_id[0] = None
        _mode[0] = "idle"

    def _switch_game(new_game_id: str, display_name: str, category: str) -> None:
        """Switch active game context. Stops any active session first."""
        if _game_id[0] == new_game_id:
            return  # same game, no-op

        # Stop practice if running
        if _practice[0] and _practice[0].is_running:
            _practice[0].is_running = False

        # Clear reference state
        _clear_ref_state()

        # Create game in DB if new (preserves existing name)
        db.upsert_game(new_game_id, display_name, category)

        # Switch context
        _game_id[0] = new_game_id
        _game_name[0] = display_name
        _scheduler[0] = None  # force re-creation for new game
        _mode[0] = "idle"

    # Expose for testing
    app.state._switch_game = _switch_game

    def _on_disconnect():
        """Handle TCP disconnect: stop practice if running, clear ref state."""
        if _practice[0] and _practice[0].is_running:
            _practice[0].is_running = False
        _clear_ref_state()

    tcp.on_disconnect = _on_disconnect

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
                # In practice mode, the practice loop consumes events — don't compete
                if _mode[0] == "practice":
                    await asyncio.sleep(0.5)
                    continue

                event = await tcp.recv_event(timeout=1.0)
                if event is None:
                    continue

                # Handle rom_info: auto-discover game from ROM filename
                if event.get("event") == "rom_info":
                    filename = event.get("filename", "")
                    if rom_dir and filename:
                        rom_path = rom_dir / filename
                        if rom_path.exists():
                            from spinlab.romid import rom_checksum, game_name_from_filename
                            checksum = rom_checksum(rom_path)
                            name = game_name_from_filename(filename)
                            _switch_game(checksum, name, default_category)
                            await tcp.send(json.dumps({
                                "event": "game_context",
                                "game_id": checksum,
                                "game_name": name,
                            }))
                        else:
                            from spinlab.romid import game_name_from_filename
                            name = game_name_from_filename(filename)
                            fallback_id = f"file_{name.lower().replace(' ', '_')}"
                            _switch_game(fallback_id, name, default_category)
                            await tcp.send(json.dumps({
                                "event": "game_context",
                                "game_id": fallback_id,
                                "game_name": name,
                            }))
                            logger.warning(
                                "ROM not found in rom_dir: %s — using filename as ID", filename
                            )
                    continue

                # Only capture events in reference mode
                if _mode[0] != "reference":
                    continue

                # Reference mode: pair transition events into splits
                evt_type = event.get("event")
                if evt_type == "level_entrance":
                    key = (event["level"], event["room"])
                    _ref_pending[key] = event

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
                        gid = _require_game()
                        split_id = Split.make_id(
                            gid, entrance["level"], entrance["room"], goal
                        )
                        split = Split(
                            id=split_id,
                            game_id=gid,
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

        # No game loaded yet
        if _game_id[0] is None:
            return {
                "mode": mode,
                "tcp_connected": tcp.is_connected,
                "game_id": None,
                "game_name": None,
                "current_split": None,
                "queue": [],
                "recent": [],
                "session": None,
                "sections_captured": 0,
                "allocator": None,
                "estimator": None,
            }

        gid = _game_id[0]
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
                splits = db.get_all_splits_with_model(gid)
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
            splits_all = db.get_all_splits_with_model(gid)
            smap = {s["id"]: s for s in splits_all}
            queue = [smap[sid] for sid in queue_ids if sid in smap]

        recent = db.get_recent_attempts(gid, limit=8)

        return {
            "mode": mode,
            "tcp_connected": tcp.is_connected,
            "game_id": _game_id[0],
            "game_name": _game_name[0],
            "current_split": current_split,
            "queue": queue,
            "recent": recent,
            "session": session_dict,
            "sections_captured": _ref_splits_count[0],
            "allocator": sched.allocator.name,
            "estimator": sched.estimator.name,
        }

    @app.post("/api/reference/start")
    def reference_start():
        if _mode[0] == "practice":
            return {"status": "practice_active"}
        if not tcp.is_connected:
            return {"status": "not_connected"}
        gid = _require_game()
        _clear_ref_state()  # reset any stale state
        run_id = f"live_{uuid.uuid4().hex[:8]}"
        run_name = f"Live {datetime.now(UTC).strftime('%Y-%m-%d %H:%M')}"
        db.create_capture_run(run_id, gid, run_name)
        db.set_active_capture_run(run_id)
        _ref_capture_run_id[0] = run_id
        _mode[0] = "reference"
        return {"status": "started", "run_id": run_id, "run_name": run_name}

    @app.post("/api/reference/stop")
    def reference_stop():
        if _mode[0] != "reference":
            return {"status": "not_in_reference"}
        _clear_ref_state()  # resets mode to idle
        return {"status": "stopped"}

    @app.post("/api/practice/start")
    async def practice_start():
        if _practice[0] and _practice[0].is_running:
            return {"status": "already_running"}
        if not tcp.is_connected:
            return {"status": "not_connected"}

        # Clean up reference state if transitioning from reference mode
        if _mode[0] == "reference":
            _clear_ref_state()

        ps = PracticeSession(tcp=tcp, db=db, game_id=_require_game())
        _practice[0] = ps
        _practice_task[0] = asyncio.create_task(ps.run_loop())
        _practice_task[0].add_done_callback(
            lambda _: _mode.__setitem__(0, "idle") if _mode[0] == "practice" else None
        )
        _mode[0] = "practice"
        return {"status": "started", "session_id": ps.session_id}

    @app.post("/api/practice/stop")
    async def practice_stop():
        if _practice[0] and _practice[0].is_running:
            _practice[0].is_running = False
            if _practice_task[0]:
                try:
                    await asyncio.wait_for(_practice_task[0], timeout=5)
                except asyncio.TimeoutError:
                    _practice_task[0].cancel()
            _mode[0] = "idle"
            return {"status": "stopped"}
        # Session already self-terminated but mode stuck — clean up
        if _mode[0] == "practice":
            _mode[0] = "idle"
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
    async def reset_data():
        # Stop active practice session if running
        if _practice[0] and _practice[0].is_running:
            _practice[0].is_running = False
            if _practice_task[0]:
                try:
                    await asyncio.wait_for(_practice_task[0], timeout=5)
                except asyncio.TimeoutError:
                    _practice_task[0].cancel()
        _clear_ref_state()  # resets mode, ref state
        gid = _game_id[0]
        if gid:
            db.reset_game_data(gid)
        _scheduler[0] = None
        return {"status": "ok"}

    @app.get("/api/splits")
    def api_splits():
        splits = db.get_all_splits_with_model(_require_game())
        return {"splits": splits}

    @app.get("/api/sessions")
    def api_sessions():
        sessions = db.get_session_history(_require_game())
        return {"sessions": sessions}

    # -- Reference management --

    @app.get("/api/references")
    def list_references():
        return {"references": db.list_capture_runs(_require_game())}

    @app.post("/api/references")
    def create_reference(body: dict):
        run_id = f"ref_{uuid.uuid4().hex[:8]}"
        name = body.get("name", "Untitled")
        db.create_capture_run(run_id, _require_game(), name)
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

    # -- Emulator launch --

    @app.post("/api/emulator/launch")
    def launch_emulator():
        import subprocess
        cfg = app.state.config
        emu_path = cfg.get("emulator", {}).get("path", "")
        if not emu_path or not Path(emu_path).exists():
            return {"status": "error", "message": f"Emulator not found: {emu_path}"}
        rom_path = cfg.get("rom", {}).get("path", "")
        lua_script = cfg.get("emulator", {}).get("lua_script", "")
        cmd = [emu_path]
        if rom_path and Path(rom_path).exists():
            cmd.append(rom_path)
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
        game_name = manifest.get("game_id", _game_id[0] or "unknown")
        seed_db_from_manifest(db, manifest, game_name)
        return {"status": "ok", "splits_imported": len(manifest.get("splits", []))}

    return app
