"""Tests for ``spinlab fit inventory`` — the diagnostic command.

Goal of the command: a single screen that tells Andrew "did this practice
session produce the data the v07 pipeline expects?". The tests pin the
output's shape so future formatter tweaks don't drop a load-bearing line.
"""
from __future__ import annotations

import argparse
import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from spinlab.db import Database


def _write_cfg(tmp_path: Path) -> tuple[Path, Path]:
    """Write a minimal config.yaml under ``data:`` (the shape AppConfig
    expects — see python/spinlab/config.py). Returns (cfg_path, data_dir)."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    cfg = tmp_path / "spinlab.yaml"
    cfg.write_text(
        f"data:\n  dir: {data_dir.as_posix()}\n"
        f"network:\n  port: 15400\n  dashboard_port: 15401\n"
    )
    return cfg, data_dir


def _seed_game(
    db: Database, game_id: str, name: str = "Test", category: str = "Any%",
) -> None:
    db.conn.execute(
        "INSERT INTO games (id, name, category, created_at) "
        "VALUES (?, ?, ?, '2026-05-19T00:00:00Z')",
        (game_id, name, category),
    )


def _seed_segment(
    db: Database, segment_id: str, game_id: str, level: int,
    active: int = 1, ordinal: int | None = None,
) -> None:
    db.conn.execute(
        "INSERT INTO segments (id, game_id, level_number, "
        "start_type, end_type, ordinal, active, created_at, updated_at) "
        "VALUES (?, ?, ?, 'entrance', 'exit', ?, ?, "
        "'2026-05-19T00:00:00Z', '2026-05-19T00:00:00Z')",
        (segment_id, game_id, level, ordinal if ordinal is not None else level, active),
    )


def _seed_session(db: Database, session_id: str, game_id: str) -> None:
    db.conn.execute(
        "INSERT INTO sessions (id, game_id, started_at) "
        "VALUES (?, ?, '2026-05-19T00:00:00Z')",
        (session_id, game_id),
    )


def _seed_event(
    db: Database, segment_id: str, outcome: str, source: str,
    session_id: str = "sess1", episode_id: str = "ep1", time_ms: int = 30000,
) -> None:
    db.conn.execute(
        """INSERT INTO attempts
           (segment_id, session_id, episode_id, outcome, time_ms,
            source, invalidated, created_at)
           VALUES (?, ?, ?, ?, ?, ?, 0, '2026-05-19T00:00:00Z')""",
        (segment_id, session_id, episode_id, outcome, time_ms, source),
    )


def _seed_fit(db: Database, segment_id: str, kind: str = "segment_fit", n: int = 10):
    payload = {
        "schema": "segments-v1", "kind": kind,
        "segment_id": segment_id, "n_attempts": n,
        "model": "haz1", "wall_time_s": 0.05,
        "status": {
            "converged": True, "band_source": "laplace",
            "laplace_pd": True, "ppc_tension": False, "fittable": True,
        },
        "result": {"derived": {"M_clear": {"median_ms": 25000,
                                           "p5_ms": 22000, "p95_ms": 28000}}},
        "caveats": [],
    }
    db.save_segment_fit(segment_id, kind, payload)  # type: ignore[arg-type]


def _populated_db(tmp_path: Path) -> tuple[Path, Database]:
    """Seed: one game `g1` with 2 segments, mixed sources, fits on one segment."""
    cfg, data_dir = _write_cfg(tmp_path)
    db = Database(data_dir / "spinlab.db")
    _seed_game(db, "g1", name="SMW", category="Any%")
    _seed_segment(db, "s1", "g1", level=1)
    _seed_segment(db, "s2", "g1", level=2)
    _seed_session(db, "sess1", "g1")
    # s1 — 6 practice events (4 survived, 2 died)
    for i in range(4):
        _seed_event(db, "s1", "survived", "practice", episode_id=f"e{i}")
    for i in range(2):
        _seed_event(db, "s1", "died", "practice", episode_id=f"d{i}")
    # s2 — 2 hyper_play events + 1 reference (all survived)
    for i in range(2):
        _seed_event(db, "s2", "survived", "hyper_play", episode_id=f"sr{i}")
    _seed_event(db, "s2", "survived", "reference")
    db.conn.commit()
    # Fit only on s1 (n>=5).
    _seed_fit(db, "s1", "segment_fit", n=6)
    _seed_fit(db, "s1", "segment_fit", n=6)  # second fit, latest
    return cfg, db


def _run_inventory(**ns_overrides) -> tuple[int, str]:
    from spinlab import cli_fit_inventory
    buf = io.StringIO()
    ns = argparse.Namespace(**{
        "json_output": False, "game": None, "all_games": False,
        **ns_overrides,
    })
    with redirect_stdout(buf):
        code = cli_fit_inventory.run(ns)
    return code, buf.getvalue()


def test_inventory_for_one_game_summarizes_counts(tmp_path):
    cfg, _ = _populated_db(tmp_path)
    code, out = _run_inventory(config=str(cfg), game="g1")
    assert code == 0
    # Game identification
    assert "g1" in out
    # Segment counts
    assert "Segments" in out
    assert "2" in out  # 2 total
    # Event totals (9 events: 6 + 3)
    assert "9" in out
    # Outcome breakdown (7 survived, 2 died)
    assert "survived" in out
    assert "died" in out
    # Source breakdown
    assert "practice" in out
    assert "hyper_play" in out
    assert "reference" in out


def test_inventory_lists_fittable_segments(tmp_path):
    cfg, _ = _populated_db(tmp_path)
    code, out = _run_inventory(config=str(cfg), game="g1")
    assert code == 0
    # s1 has 6 events (>=5) so it should appear in the fittable list.
    # s2 has only 3 events, should not.
    fittable_section = out.split("Fittable")[-1]
    assert "s1" in fittable_section
    # s2 might appear elsewhere (in source/segment counts) but not in
    # the fittable list specifically.
    assert "s2" not in fittable_section.split("Fits stored")[0]


def test_inventory_shows_fit_counts(tmp_path):
    cfg, _ = _populated_db(tmp_path)
    code, out = _run_inventory(config=str(cfg), game="g1")
    assert code == 0
    # Two segment_fit rows seeded; zero pool_fit.
    assert "2 segment_fit" in out
    assert "0 pool_fit" in out


def test_inventory_handles_game_with_zero_attempts(tmp_path):
    cfg, data_dir = _write_cfg(tmp_path)
    db = Database(data_dir / "spinlab.db")
    _seed_game(db, "g_empty")
    _seed_segment(db, "s1", "g_empty", level=1)
    db.conn.commit()

    code, out = _run_inventory(config=str(cfg), game="g_empty")
    assert code == 0
    assert "g_empty" in out
    # No events at all — the output should say so explicitly rather than
    # rendering an empty section that's ambiguous with "I forgot to query".
    assert "0" in out
    assert "no" in out.lower() or "empty" in out.lower() or "0 events" in out.lower() \
        or "Event attempts:  0" in out


def test_inventory_errors_when_game_not_found(tmp_path):
    cfg, data_dir = _write_cfg(tmp_path)
    Database(data_dir / "spinlab.db")  # empty DB, no games

    code, out = _run_inventory(config=str(cfg), game="nonexistent")
    # Nonzero exit so scripts can detect "I asked for a real game and it
    # didn't exist" rather than silently emitting an empty report.
    assert code == 1
    assert "nonexistent" in out


def test_inventory_all_games_iterates_every_game(tmp_path):
    cfg, data_dir = _write_cfg(tmp_path)
    db = Database(data_dir / "spinlab.db")
    _seed_game(db, "g1", name="SMW")
    _seed_game(db, "g2", name="SMA1")
    _seed_segment(db, "s1", "g1", level=1)
    _seed_segment(db, "s2", "g2", level=1)
    db.conn.commit()

    code, out = _run_inventory(config=str(cfg), all_games=True)
    assert code == 0
    assert "g1" in out
    assert "g2" in out


def test_inventory_all_games_with_empty_db_exits_zero(tmp_path):
    """No games at all is informational, not an error — fresh post-reset
    DBs hit this path before the first reference run."""
    cfg, data_dir = _write_cfg(tmp_path)
    Database(data_dir / "spinlab.db")
    code, out = _run_inventory(config=str(cfg), all_games=True)
    assert code == 0
    assert "no games" in out.lower()


def test_inventory_requires_game_or_all(tmp_path):
    """Neither --game nor --all → error (we never want to silently
    pick a single arbitrary game)."""
    cfg, data_dir = _write_cfg(tmp_path)
    Database(data_dir / "spinlab.db")
    code, out = _run_inventory(config=str(cfg))  # both default to None/False
    assert code == 2
    assert "--game" in out or "--all" in out


def test_inventory_json_output_is_valid_object(tmp_path):
    cfg, _ = _populated_db(tmp_path)
    code, out = _run_inventory(config=str(cfg), game="g1", json_output=True)
    assert code == 0
    parsed = json.loads(out)
    # JSON shape: a dict with `games` key whose value is a list of
    # per-game records, even for the single-game case (uniform with --all).
    assert isinstance(parsed, dict)
    assert "games" in parsed
    assert len(parsed["games"]) == 1
    g = parsed["games"][0]
    assert g["game_id"] == "g1"
    assert g["segments"]["total"] == 2
    assert g["events"]["total"] == 9
    assert g["events"]["by_outcome"]["survived"] == 7
    assert g["events"]["by_outcome"]["died"] == 2
    assert g["events"]["by_source"]["practice"] == 6
    assert g["events"]["by_source"]["hyper_play"] == 2
    assert g["events"]["by_source"]["reference"] == 1
    assert g["fittable"]["count"] == 1
    assert g["fittable"]["segments"][0]["segment_id"] == "s1"
    assert g["fits"]["segment_fit"] == 2
    assert g["fits"]["pool_fit"] == 0


def test_inventory_json_all_games_returns_multiple_entries(tmp_path):
    cfg, data_dir = _write_cfg(tmp_path)
    db = Database(data_dir / "spinlab.db")
    _seed_game(db, "g1")
    _seed_game(db, "g2")
    _seed_segment(db, "s1", "g1", level=1)
    _seed_segment(db, "s2", "g2", level=1)
    db.conn.commit()

    code, out = _run_inventory(config=str(cfg), all_games=True, json_output=True)
    assert code == 0
    parsed = json.loads(out)
    ids = {g["game_id"] for g in parsed["games"]}
    assert ids == {"g1", "g2"}
