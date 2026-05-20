"""In-process tests for `spinlab fit show` and `spinlab fit list`.

Drives the runner functions directly with constructed argparse
Namespaces. End-to-end subprocess coverage lives in
`tests/integration/test_cli_fit_subprocess.py`.
"""
from __future__ import annotations

import argparse
import io
import json
from contextlib import redirect_stdout

import pytest

from spinlab.db import Database


def _seed_db(tmp_path):
    """Seed one game, two segments, one fit on each.

    Writes a minimal config.yaml under ``data:`` (the shape AppConfig
    expects — see python/spinlab/config.py). The DB lives at
    ``<data_dir>/spinlab.db`` so the CLI helpers can find it.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    cfg_path = tmp_path / "spinlab.yaml"
    cfg_path.write_text(
        f"data:\n  dir: {data_dir.as_posix()}\n"
        f"network:\n  port: 15400\n  dashboard_port: 15401\n"
    )
    db = Database(data_dir / "spinlab.db")
    db.conn.execute(
        "INSERT INTO games (id, name, category, created_at) "
        "VALUES ('g1', 'Test', 'Any%', '2026-05-19T00:00:00Z')"
    )
    for sid, lvl in [("s1", 1), ("s2", 2)]:
        db.conn.execute(
            "INSERT INTO segments (id, game_id, level_number, "
            "start_type, end_type, ordinal, created_at, updated_at) "
            "VALUES (?, 'g1', ?, 'entrance', 'exit', ?, "
            "'2026-05-19T00:00:00Z', '2026-05-19T00:00:00Z')",
            (sid, lvl, lvl),
        )
    db.conn.commit()

    payload_s1 = {
        "schema": "segments-v1", "kind": "segment_fit",
        "segment_id": "s1", "n_attempts": 30,
        "model": "haz1", "wall_time_s": 0.05,
        "status": {
            "converged": True, "band_source": "laplace",
            "laplace_pd": True, "ppc_tension": False, "fittable": True,
        },
        "result": {
            "map": {"log_theta": [9.9] + [0.0] * 9, "natural": {}},
            "bands": {"log_bpt": {"p5": 9.85, "p50": 9.9, "p95": 9.95}},
            "derived": {
                "M_clear": {"median_ms": 25000, "p5_ms": 22000, "p95_ms": 28000},
                "death_rate_next": 0.18,
            },
            "ppc": {},
        },
        "caveats": [],
    }
    db.save_segment_fit("s1", "segment_fit", payload_s1)
    payload_s2 = {**payload_s1, "segment_id": "s2", "n_attempts": 10,
                  "result": {**payload_s1["result"],
                             "derived": {"M_clear": {"median_ms": 40000,
                                                     "p5_ms": 35000, "p95_ms": 50000},
                                         "death_rate_next": 0.45}}}
    db.save_segment_fit("s2", "segment_fit", payload_s2)
    return cfg_path


def _run(cmd: str, **ns_overrides) -> tuple[int, str]:
    """Invoke the named runner with a constructed Namespace; capture stdout."""
    from spinlab import cli_fit
    runner = {"show": cli_fit.run_show, "list": cli_fit.run_list}[cmd]
    ns = argparse.Namespace(**ns_overrides)
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = runner(ns)
    return code, buf.getvalue()


def test_fit_show_prints_payload_for_existing_segment(tmp_path):
    cfg_path = _seed_db(tmp_path)
    code, out = _run(
        "show",
        config=str(cfg_path), segment_id="s1",
        kind="segment_fit", history=None, json_output=False, game="g1",
    )
    assert code == 0
    assert "s1" in out
    assert "n_attempts: 30" in out
    assert "M_clear median: 25000 ms" in out
    assert "fittable: yes" in out


def test_fit_show_json_dumps_raw_payload(tmp_path):
    cfg_path = _seed_db(tmp_path)
    code, out = _run(
        "show",
        config=str(cfg_path), segment_id="s1",
        kind="segment_fit", history=None, json_output=True, game="g1",
    )
    assert code == 0
    parsed = json.loads(out)
    assert parsed["segment_id"] == "s1"
    assert parsed["n_attempts"] == 30
    assert parsed["status"]["fittable"] is True


def test_fit_show_returns_nonzero_when_segment_has_no_fit(tmp_path):
    cfg_path = _seed_db(tmp_path)
    code, out = _run(
        "show",
        config=str(cfg_path), segment_id="nonexistent",
        kind="segment_fit", history=None, json_output=False, game="g1",
    )
    assert code == 1
    assert "no fit found" in out.lower()


def test_fit_show_history_prints_one_line_per_recent_fit(tmp_path):
    cfg_path = _seed_db(tmp_path)
    # Write two more fits on s1 so we have 3 total.
    db = Database(tmp_path / "data" / "spinlab.db")
    for n in (31, 32):
        db.save_segment_fit("s1", "segment_fit", {
            "schema": "segments-v1", "kind": "segment_fit",
            "segment_id": "s1", "n_attempts": n,
            "model": "haz1", "wall_time_s": 0.05,
            "status": {"converged": True, "band_source": "laplace",
                       "laplace_pd": True, "ppc_tension": False, "fittable": True},
            "result": {"derived": {"M_clear": {"median_ms": 25000 - n,
                                               "p5_ms": 0, "p95_ms": 0}}},
            "caveats": [],
        })
    code, out = _run(
        "show",
        config=str(cfg_path), segment_id="s1",
        kind="segment_fit", history=10, json_output=False, game="g1",
    )
    assert code == 0
    # Three fits → three lines (newest first).
    lines = [ln for ln in out.splitlines() if ln.startswith("20")]
    assert len(lines) == 3
    # Each line carries an n= field.
    assert all("n=" in ln for ln in lines)


def test_fit_list_renders_one_row_per_segment_with_fits(tmp_path):
    cfg_path = _seed_db(tmp_path)
    code, out = _run(
        "list",
        config=str(cfg_path), game="g1",
        kind="segment_fit", json_output=False,
    )
    assert code == 0
    # A header line + two data rows.
    data_lines = [ln for ln in out.splitlines() if "\t" in ln]
    assert len(data_lines) >= 2
    sids = {ln.split("\t")[0].strip() for ln in data_lines}
    assert {"s1", "s2"} <= sids


def test_fit_list_json_returns_array_of_summaries(tmp_path):
    cfg_path = _seed_db(tmp_path)
    code, out = _run(
        "list",
        config=str(cfg_path), game="g1",
        kind="segment_fit", json_output=True,
    )
    assert code == 0
    parsed = json.loads(out)
    assert isinstance(parsed, list)
    sids = {row["segment_id"] for row in parsed}
    assert sids == {"s1", "s2"}


def test_fit_list_empty_game_prints_message_and_exits_zero(tmp_path):
    """A game with no fits should exit 0 with an informational message
    — not crash, not return nonzero."""
    cfg_path = _seed_db(tmp_path)
    # Reuse the seeded config but pass a game id that has no rows.
    code, out = _run(
        "list",
        config=str(cfg_path), game="g-empty",
        kind="segment_fit", json_output=False,
    )
    assert code == 0
    assert "no fits found" in out.lower()
