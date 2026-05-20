"""End-to-end smoke test: ``python -m spinlab.cli fit ...`` exits cleanly.

Unit tests in ``tests/unit/test_cli_fit.py`` drive the runner functions
directly; this file confirms the packaging-level wiring still works —
argparse parses, ``cli.py`` routes to the ``fit`` subparser, ``--config``
resolves, and exit codes propagate out of the subprocess.

YAML shape note: ``AppConfig.from_yaml`` expects ``data.dir`` (nested),
not a top-level ``data_dir`` — matching the seeding pattern used by
``tests/unit/test_cli_fit.py`` and ``tests/integration/test_fit_pool_cli.py``.
"""
from __future__ import annotations

import subprocess
import sys

from spinlab.db import Database


def _seed(tmp_path):
    """Seed config + DB with one game, one segment, one fit on ``s1``.

    Returns the config-file path so the subprocess can pick it up via
    ``--config``. The DB lives at ``<data_dir>/spinlab.db`` so the CLI's
    ``_open_db`` helper finds it via ``cfg.data_dir``.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    cfg = tmp_path / "spinlab.yaml"
    cfg.write_text(
        f"data:\n  dir: {data_dir.as_posix()}\n"
        f"network:\n  port: 15400\n  dashboard_port: 15401\n"
    )
    db = Database(data_dir / "spinlab.db")
    db.conn.execute(
        "INSERT INTO games (id, name, category, created_at) "
        "VALUES ('g1', 'Test', 'Any%', '2026-05-19T00:00:00Z')"
    )
    db.conn.execute(
        "INSERT INTO segments (id, game_id, level_number, "
        "start_type, end_type, ordinal, created_at, updated_at) "
        "VALUES ('s1', 'g1', 1, 'entrance', 'exit', 1, "
        "'2026-05-19T00:00:00Z', '2026-05-19T00:00:00Z')"
    )
    payload = {
        "schema": "segments-v1", "kind": "segment_fit",
        "segment_id": "s1", "n_attempts": 10, "model": "haz1",
        "wall_time_s": 0.05,
        "status": {
            "converged": True, "band_source": "laplace",
            "laplace_pd": True, "ppc_tension": False, "fittable": True,
        },
        "result": {
            "map": {"log_theta": [9.9] + [0.0] * 9, "natural": {}},
            "bands": {},
            "derived": {
                "M_clear": {"median_ms": 25000, "p5_ms": 22000, "p95_ms": 28000},
                "death_rate_next": 0.18,
            },
            "ppc": {},
        },
        "caveats": [],
    }
    db.save_segment_fit("s1", "segment_fit", payload)
    db.conn.commit()
    return cfg


def test_fit_show_subprocess_round_trip(tmp_path):
    cfg = _seed(tmp_path)
    result = subprocess.run(
        [sys.executable, "-m", "spinlab.cli", "fit", "show", "s1",
         "--config", str(cfg)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, (
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert "s1" in result.stdout
    assert "M_clear median: 25000 ms" in result.stdout


def test_fit_list_subprocess_round_trip(tmp_path):
    cfg = _seed(tmp_path)
    result = subprocess.run(
        [sys.executable, "-m", "spinlab.cli", "fit", "list",
         "--game", "g1", "--config", str(cfg)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, (
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert "segment_id" in result.stdout  # header
    assert "s1" in result.stdout
