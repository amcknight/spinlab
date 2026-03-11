"""Reference capture: parse passive JSONL log into a practice manifest."""
from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

import yaml

from spinlab.models import Split


def parse_log(lines: list[str]) -> list[dict[str, Any]]:
    """Parse JSONL lines into a list of event dicts, skipping blank lines."""
    events = []
    for line in lines:
        line = line.strip()
        if line:
            events.append(json.loads(line))
    return events


def pair_events(
    events: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Pair each level_entrance with the next level_exit for the same (level, room).

    Unpaired entrances (run abandoned) are silently dropped.
    Deaths and other events are ignored.
    """
    pairs: list[tuple[dict, dict]] = []
    pending: dict[tuple[int, int], dict] = {}  # (level, room) -> entrance event

    for event in events:
        evt = event.get("event")
        if evt == "level_entrance":
            key = (event["level"], event["room"])
            pending[key] = event
        elif evt == "level_exit":
            key = (event["level"], event["room"])
            if key in pending:
                pairs.append((pending.pop(key), event))

    return pairs


def build_manifest(
    pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    *,
    game_id: str,
    category: str,
) -> dict[str, Any]:
    """Build manifest dict from paired (entrance, exit) events."""
    splits = []
    for entr, ex in pairs:
        split_id = Split.make_id(
            game_id, entr["level"], entr["room"], ex["goal"]
        )
        splits.append(
            {
                "id": split_id,
                "level_number": entr["level"],
                "room_id": entr["room"],
                "goal": ex["goal"],
                "state_path": entr.get("state_path"),
                "reference_time_ms": ex["elapsed_ms"],
            }
        )
    return {
        "game_id": game_id,
        "category": category,
        "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "splits": splits,
    }


def main() -> None:
    """CLI entry point: read config, parse log, write manifest."""
    config_path = Path("config.yaml")
    if not config_path.exists():
        raise SystemExit("config.yaml not found — run from repo root")

    with config_path.open() as f:
        config = yaml.safe_load(f)

    game_id: str = config["game"]["id"]
    category: str = config["game"]["category"]
    script_data_dir = Path(config["emulator"]["script_data_dir"])
    data_dir = Path(config["data"]["dir"])

    log_path = script_data_dir / "passive_log.jsonl"
    if not log_path.exists():
        raise SystemExit(f"Log not found: {log_path}")

    lines = log_path.read_text(encoding="utf-8").splitlines()
    events = parse_log(lines)
    pairs = pair_events(events)
    manifest = build_manifest(pairs, game_id=game_id, category=category)

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_path = data_dir / "captures" / f"{date_str}_{game_id}_manifest.yaml"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with out_path.open("w", encoding="utf-8") as f:
        yaml.dump(manifest, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

    print(f"Wrote {len(manifest['splits'])} splits -> {out_path}")


if __name__ == "__main__":
    main()
