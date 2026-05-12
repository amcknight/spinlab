"""Tests for the POST /api/practice/invalidate endpoint."""

from fastapi.testclient import TestClient

from spinlab.config import AppConfig, EmulatorConfig, NetworkConfig
from spinlab.dashboard import create_app
from spinlab.db import Database


def test_invalidate_returns_ok_when_no_attempt(tmp_path):
    """No active attempt → endpoint still returns ok (handler is a no-op for missing data)."""
    db = Database(":memory:")
    cfg = AppConfig(
        network=NetworkConfig(),
        emulator=EmulatorConfig(
            savestate_dir=tmp_path / "ra",
            spinlab_state_dir=tmp_path / "sl",
        ),
        data_dir=tmp_path,
        rom_dir=None,
    )
    app = create_app(db, config=cfg)
    with TestClient(app) as client:
        resp = client.post("/api/practice/invalidate")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
