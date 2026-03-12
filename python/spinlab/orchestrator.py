"""SpinLab practice session orchestrator."""
from __future__ import annotations

import json
import os
import socket
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

import yaml
from datetime import datetime

from .db import Database
from .models import Attempt, Rating, Split, SplitCommand
from .scheduler import Scheduler


def write_state_file(
    path: Path,
    session_id: str,
    started_at: str,
    current_split_id: str,
    queue: list[str],
) -> None:
    """Atomically write orchestrator state for dashboard consumption."""
    state = {
        "session_id": session_id,
        "started_at": started_at,
        "current_split_id": current_split_id,
        "queue": queue,
        "updated_at": datetime.utcnow().isoformat() + "Z",
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    tmp.replace(path)


def clear_state_file(path: Path) -> None:
    """Remove state file when session ends."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _parse_attempt_result_from_buffer(buf: str) -> tuple[Optional[dict], str]:
    """Parse one attempt_result JSON event from the buffer.

    Returns (result_dict, remaining_buf) if found, or (None, buf) if not enough data.
    Discards non-JSON lines and JSON lines that aren't attempt_result events.
    """
    while "\n" in buf:
        line, buf = buf.split("\n", 1)
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
            if msg.get("event") == "attempt_result":
                return msg, buf
        except json.JSONDecodeError:
            pass  # discard plain-text responses like ok:queued, pong
    return None, buf


def find_latest_manifest(data_dir: Path) -> Optional[Path]:
    """Return the most-recently-named manifest YAML, or None if none exist."""
    captures = list((data_dir / "captures").glob("*_manifest.yaml"))
    if not captures:
        return None
    return sorted(captures)[-1]  # date-prefixed filenames sort correctly


def load_manifest(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def seed_db_from_manifest(db: Database, manifest: dict, game_name: str) -> None:
    """Upsert game + all splits from manifest into the DB.

    Does NOT create schedule entries — that is Scheduler.init_schedules()'s job,
    called separately in run() after seeding.
    """
    game_id: str = manifest["game_id"]
    category: str = manifest.get("category", "any%")
    db.upsert_game(game_id, game_name, category)

    for entry in manifest["splits"]:
        split = Split(
            id=entry["id"],
            game_id=game_id,
            level_number=entry["level_number"],
            room_id=entry.get("room_id"),
            goal=entry["goal"],
            description=entry.get("name", ""),
            state_path=entry.get("state_path"),
            reference_time_ms=entry.get("reference_time_ms"),
        )
        db.upsert_split(split)


def connect_to_lua(host: str, port: int, timeout: float = 30.0) -> socket.socket:
    """Connect to Lua TCP server, retrying every 0.5s until timeout."""
    deadline = time.monotonic() + timeout
    last_err: Optional[Exception] = None
    while time.monotonic() < deadline:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.connect((host, port))
            return sock
        except OSError as e:
            last_err = e
            sock.close()
            time.sleep(0.5)
    raise ConnectionError(f"Could not connect to Lua on {host}:{port}") from last_err


def send_line(sock: socket.socket, msg: str) -> None:
    sock.sendall((msg + "\n").encode("utf-8"))


def recv_until_attempt_result(sock: socket.socket) -> dict:
    """Block until Lua pushes an attempt_result event. No timeout."""
    buf = ""
    while True:
        chunk = sock.recv(4096).decode("utf-8")
        if not chunk:
            raise ConnectionError("TCP socket closed while waiting for attempt_result")
        buf += chunk
        result, buf = _parse_attempt_result_from_buffer(buf)
        if result is not None:
            return result


def run(config_path: Path = Path("config.yaml")) -> None:
    # -- Config --
    with config_path.open(encoding="utf-8") as f:
        config = yaml.safe_load(f)

    game_id: str = config["game"]["id"]
    game_name: str = config["game"]["name"]
    host: str = config["network"]["host"]
    port: int = config["network"]["port"]
    base_interval: float = float(config["scheduler"]["base_interval_minutes"])
    data_dir = Path(config["data"]["dir"])
    state_file = data_dir / "orchestrator_state.json"

    # -- Manifest → DB --
    manifest_path = find_latest_manifest(data_dir)
    if not manifest_path:
        sys.exit(f"No manifest found in {data_dir / 'captures'} — run capture first.")

    db = Database(data_dir / "spinlab.db")
    manifest = load_manifest(manifest_path)
    seed_db_from_manifest(db, manifest, game_name)

    scheduler = Scheduler(db, game_id, base_interval)
    scheduler.init_schedules()

    if not db.get_active_splits(game_id):
        sys.exit("No active splits in DB — check manifest.")

    # -- Connect --
    print(f"Connecting to Lua on {host}:{port} (waiting up to 30s)...")
    sock = connect_to_lua(host, port)

    # Ping to verify connection
    send_line(sock, "ping")
    buf = ""
    while "pong" not in buf:
        buf += sock.recv(256).decode("utf-8")
    print("Connected.")

    # -- Session --
    session_id = uuid.uuid4().hex
    db.create_session(session_id, game_id)
    session_started_at = datetime.utcnow().isoformat() + "Z"
    splits_attempted = 0
    splits_completed = 0
    # Track splits skipped this session due to missing state files.
    # If every active split ends up skipped, exit rather than infinite-loop.
    session_skipped: set[str] = set()

    try:
        while True:
            cmd = scheduler.pick_next()
            if cmd is None:
                print("No splits available — exiting.")
                break

            if cmd.state_path and not os.path.exists(cmd.state_path):
                session_skipped.add(cmd.id)
                print(f"[warn] Missing state file: {cmd.state_path} — skipping {cmd.id}")
                active = db.get_active_splits(game_id)
                if all(s.id in session_skipped for s in active):
                    sys.exit("All splits have missing state files — exiting.")
                continue

            send_line(sock, "practice_load:" + json.dumps(cmd.to_dict()))
            queue = scheduler.peek_next_n(3)
            queue = [q for q in queue if q != cmd.id][:2]
            write_state_file(state_file, session_id, session_started_at, cmd.id, queue)
            result = recv_until_attempt_result(sock)

            rating = Rating(result["rating"])
            attempt = Attempt(
                split_id=result["split_id"],
                session_id=session_id,
                completed=result["completed"],
                time_ms=result.get("time_ms"),
                goal_matched=(result.get("goal") == cmd.goal) if result.get("completed") else None,
                rating=rating,
                source="practice",
            )
            db.log_attempt(attempt)
            scheduler.process_rating(result["split_id"], rating)

            splits_attempted += 1
            if result["completed"]:
                splits_completed += 1

            status = "✓" if result["completed"] else "✗"
            label = cmd.description if cmd.description else result["split_id"]
            print(f"{status} {label}  {rating.value}  "
                  f"{result.get('time_ms', '?')}ms")

    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        try:
            send_line(sock, "practice_stop")
            sock.recv(64)  # drain ok response
        except OSError:
            pass
        sock.close()
        db.end_session(session_id, splits_attempted, splits_completed)
        clear_state_file(state_file)
        db.close()
        print(f"Session ended: {splits_attempted} attempts, {splits_completed} completed.")


if __name__ == "__main__":
    run()
