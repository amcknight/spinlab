"""POST /api/emulator/launch behavior under both backends."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from spinlab.config import AppConfig, EmulatorConfig, NetworkConfig
from spinlab.dashboard import create_app
from spinlab.db import Database


def _app(tmp_path: Path, *, backend: str, emu_path: Path | None = None,
         retroarch_paths: bool = False) -> TestClient:
    db = Database(":memory:")
    emu_kwargs = {"backend": backend}
    if emu_path is not None:
        emu_kwargs["path"] = emu_path
    if retroarch_paths:
        emu_kwargs.update(
            savestate_dir=tmp_path / "ra",
            spinlab_state_dir=tmp_path / "sl",
            ra_game_basename="Test",
        )
    cfg = AppConfig(
        network=NetworkConfig(),
        emulator=EmulatorConfig(**emu_kwargs),
        data_dir=tmp_path,
        rom_dir=None,
    )
    return TestClient(create_app(db, config=cfg))


def test_launch_emulator_retroarch_returns_501(tmp_path):
    """Under retroarch backend, the launcher must NOT spawn anything; 501 with a hint."""
    with _app(tmp_path, backend="retroarch", retroarch_paths=True) as client:
        resp = client.post("/api/emulator/launch", json={"rom": "doesnt_matter.smc"})
    assert resp.status_code == 501
    detail = resp.json()["detail"]
    assert "retroarch" in detail.lower()
    assert "manual" in detail.lower() or "yourself" in detail.lower()
    assert "launch-retroarch" in detail.lower()


def test_launch_emulator_mesen_lua_unchanged_path_check(tmp_path):
    """Under mesen-lua backend, missing emulator.path still 400s as before — no regression."""
    with _app(tmp_path, backend="mesen-lua", emu_path=None) as client:
        resp = client.post("/api/emulator/launch", json={"rom": "x.smc"})
    # path is None → "Emulator not found" 400, same as pre-fix.
    assert resp.status_code == 400
    assert "Emulator not found" in resp.json()["detail"]
