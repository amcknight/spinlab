"""SpinLab practice session orchestrator."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional

import yaml

from .db import Database
from .models import Split


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
            state_path=entry.get("state_path"),
            reference_time_ms=entry.get("reference_time_ms"),
        )
        db.upsert_split(split)
