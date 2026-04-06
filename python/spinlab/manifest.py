"""Manifest utilities: find, load, and seed DB from reference manifests."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Optional

import yaml

from .db import Database
from .models import Segment


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
    """Upsert game + all segments from manifest into the DB.

    Creates a capture_run for the manifest if one doesn't exist for these segments.
    Supports both 'segments' and legacy 'splits' manifest keys.
    """
    game_id: str = manifest["game_id"]
    category: str = manifest.get("category", "any%")
    db.upsert_game(game_id, game_name, category)

    # Create a capture_run for this manifest
    captured_at = manifest.get("captured_at", datetime.now(UTC).isoformat())
    run_id = f"manifest_{uuid.uuid4().hex[:8]}"
    run_name = f"Capture {captured_at[:10]}"
    db.create_capture_run(run_id, game_id, run_name)
    db.set_active_capture_run(run_id)

    # Support both 'segments' (new) and 'splits' (legacy) manifest keys
    entries = manifest.get("segments", manifest.get("splits", []))

    for idx, entry in enumerate(entries, start=1):
        seg = Segment(
            id=entry["id"],
            game_id=game_id,
            level_number=entry["level_number"],
            start_type=entry.get("start_type", "entrance"),
            start_ordinal=entry.get("start_ordinal", 0),
            end_type=entry.get("end_type", "goal"),
            end_ordinal=entry.get("end_ordinal", 0),
            description=entry.get("name", ""),
            ordinal=idx,
            reference_id=run_id,
        )
        db.upsert_segment(seg)
        # TODO(Task 10): attach save state to start waypoint once manifest
        # creates proper Waypoint objects with conditions.
        # Manifest import skips save state attachment for now.
