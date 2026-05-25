"""End-to-end test for the `spinlab fit render` CLI.

Seeds a small DB via the production helpers (so we exercise the same
write paths the real pipeline uses), runs the CLI as a subprocess to
exercise the parser + dispatcher, asserts the output file is
well-formed HTML with the expected segments.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from spinlab.db import Database
from spinlab.models import EndpointType, Segment


def _write_config(tmp_path: Path) -> Path:
    """Mirror the config schema used by other CLI tests (see test_cli_db_reset)."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config = {
        "data": {"dir": str(data_dir)},
        "network": {"port": 15482, "dashboard_port": 15483},
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.dump(config), encoding="utf-8")
    return config_path


def _make_fittable_payload(segment_id: str, n: int) -> dict:
    """Construct a minimal but renderer-complete v1 envelope."""
    bands = {
        f"log_{k}": {"p5": -0.1, "p50": 0.0, "p95": 0.1}
        for k in (
            "bpt", "sf_inf", "ssp_inf", "alpha_inf",
            "sf_1", "ssp_1", "alpha_1",
            "hl_sf", "hl_ssp", "hl_alpha",
        )
    }
    return {
        "schema": "segments-v1", "kind": "segment_fit",
        "segment_id": segment_id, "n_attempts": n, "model": "haz1",
        "wall_time_s": 0.05,
        "status": {
            "converged": True, "band_source": "laplace",
            "laplace_pd": True, "ppc_tension": False, "fittable": True,
        },
        "result": {
            "map": {"log_theta": [0.0] * 10, "natural": {}},
            "bands": bands,
            "derived": {
                "M_clear": {"median_ms": 25000.0, "p5_ms": 20000.0, "p95_ms": 35000.0},
                "death_rate_next": 0.4,
            },
            "ppc": {
                "died_rate": {"obs": 0.4, "p_two_sided": 0.5},
                "died_tau_skew": {"obs": 0.0, "p_two_sided": 0.5},
                "died_tau_kurt": {"obs": 0.0, "p_two_sided": 0.5},
                "died_s_mid_third": {"obs": 0.3, "p_two_sided": 0.5},
            },
        },
        "caveats": [],
    }


@pytest.fixture
def seeded_db(tmp_path: Path):
    """Create a config.yaml + DB with one game, one segment, one fit, two events.

    All writes go through production helpers so this fixture also
    serves as a smoke test that the schema + helpers haven't drifted.
    """
    config_path = _write_config(tmp_path)
    data_dir = tmp_path / "data"
    db = Database(str(data_dir / "spinlab.db"))

    db.upsert_game("game1", "TestGame", "any%")
    seg = Segment(
        id="game1:49:checkpoint.1:checkpoint.2:abcd1234:ef01abcd",
        game_id="game1", level_number=49,
        start_type=EndpointType.CHECKPOINT, start_ordinal=1,
        end_type=EndpointType.CHECKPOINT, end_ordinal=2,
    )
    db.upsert_segment(seg)
    db.save_segment_fit(seg.id, "segment_fit", _make_fittable_payload(seg.id, 5))
    # No attempt rows — the renderer handles an empty events list with a
    # placeholder SVG; inserting rows would require a sessions FK.
    db.close()
    return config_path, "game1"


def test_render_writes_valid_html(seeded_db, tmp_path: Path):
    config_path, game_id = seeded_db
    out_path = tmp_path / "report.html"
    result = subprocess.run(
        [sys.executable, "-m", "spinlab.cli", "fit", "render",
         "--config", str(config_path),
         "--game", game_id,
         "--out", str(out_path)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        f"CLI failed:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )
    assert out_path.exists()
    html = out_path.read_text(encoding="utf-8")
    # Game title rendered
    assert "TestGame" in html
    # Segment section rendered
    assert "seg-49-checkpoint_1-checkpoint_2" in html
    # Well-formed enough that opening/closing tag counts balance.
    # Full HTML parsing isn't worth a bs4 dep; this catches gross
    # malformedness like unclosed tags.
    assert html.count("<section") == html.count("</section>")
    assert html.count("<table") == html.count("</table>")


def test_render_no_fits_for_game_exits_nonzero(tmp_path: Path):
    """Empty-fits path: the CLI exits 1 and prints a message."""
    config_path = _write_config(tmp_path)
    data_dir = tmp_path / "data"
    db = Database(str(data_dir / "spinlab.db"))
    db.upsert_game("emptygame", "Empty", "any%")
    db.close()

    out_path = tmp_path / "report.html"
    result = subprocess.run(
        [sys.executable, "-m", "spinlab.cli", "fit", "render",
         "--config", str(config_path),
         "--game", "emptygame",
         "--out", str(out_path)],
        capture_output=True, text=True,
    )
    assert result.returncode == 1
    assert "no fits found" in result.stdout
    assert not out_path.exists()
