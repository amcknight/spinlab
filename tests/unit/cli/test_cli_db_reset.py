"""Tests for the 'spinlab db reset' CLI command."""
from pathlib import Path

import yaml
import pytest

from spinlab.cli import main


def _write_config(tmp_path: Path) -> Path:
    """Write a minimal config.yaml pointing data.dir at tmp_path/data."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config = {
        "data": {"dir": str(data_dir)},
        "network": {"port": 15482, "dashboard_port": 15483},
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.dump(config), encoding="utf-8")
    return config_path


def test_db_reset_creates_fresh_db(tmp_path):
    """'spinlab db reset' deletes existing DB and creates a fresh one."""
    config_path = _write_config(tmp_path)
    data_dir = tmp_path / "data"

    from spinlab.db import Database
    db = Database(str(data_dir / "spinlab.db"))
    db.upsert_game("g1", "Test", "any%")
    db.close()
    assert (data_dir / "spinlab.db").exists()

    main(["db", "reset", "--config", str(config_path)])

    db2 = Database(str(data_dir / "spinlab.db"))
    rows = db2.conn.execute("SELECT * FROM games").fetchall()
    assert len(rows) == 0
    db2.close()


def test_db_reset_no_existing_db(tmp_path):
    """'spinlab db reset' with no existing DB still creates a fresh one."""
    config_path = _write_config(tmp_path)
    data_dir = tmp_path / "data"
    assert not (data_dir / "spinlab.db").exists()

    main(["db", "reset", "--config", str(config_path)])

    assert (data_dir / "spinlab.db").exists()
