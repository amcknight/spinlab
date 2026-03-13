"""Manifest utilities: find, load, and seed DB from reference manifests."""
from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

from .db import Database
from .models import Split


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

    Creates a capture_run for the manifest if one doesn't exist for these splits.
    """
    game_id: str = manifest["game_id"]
    category: str = manifest.get("category", "any%")
    db.upsert_game(game_id, game_name, category)

    # Create a capture_run for this manifest
    captured_at = manifest.get("captured_at", datetime.utcnow().isoformat())
    run_id = f"manifest_{uuid.uuid4().hex[:8]}"
    run_name = f"Capture {captured_at[:10]}"
    db.create_capture_run(run_id, game_id, run_name)
    db.set_active_capture_run(run_id)

    for idx, entry in enumerate(manifest["splits"], start=1):
        split = Split(
            id=entry["id"],
            game_id=game_id,
            level_number=entry["level_number"],
            room_id=entry.get("room_id"),
            goal=entry["goal"],
            description=entry.get("name", ""),
            state_path=entry.get("state_path"),
            reference_time_ms=entry.get("reference_time_ms"),
            ordinal=idx,
            reference_id=run_id,
        )
        db.upsert_split(split)
