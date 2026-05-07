"""Tests verifying dashboard.create_app instantiates the right backend."""
from pathlib import Path

from spinlab.config import AppConfig, EmulatorConfig, NetworkConfig, PracticeConfig
from spinlab.dashboard import create_app
from spinlab.db import Database
from spinlab.retroarch.orchestrator import RetroArchOrchestrator
from spinlab.tcp_manager import TcpManager


def _make_db(tmp_path: Path) -> Database:
    """Fresh in-memory DB."""
    return Database(":memory:")


def test_create_app_default_uses_tcp_manager(tmp_path):
    """When no config is provided, defaults to mesen-lua → TcpManager."""
    db = _make_db(tmp_path)
    app = create_app(db, config=None)
    assert isinstance(app.state.tcp, TcpManager)


def test_create_app_mesen_lua_uses_tcp_manager(tmp_path):
    db = _make_db(tmp_path)
    cfg = AppConfig(
        network=NetworkConfig(),
        emulator=EmulatorConfig(backend="mesen-lua"),
        data_dir=tmp_path,
        rom_dir=None,
    )
    app = create_app(db, config=cfg)
    assert isinstance(app.state.tcp, TcpManager)


def test_create_app_retroarch_uses_orchestrator(tmp_path):
    db = _make_db(tmp_path)
    cfg = AppConfig(
        network=NetworkConfig(),
        emulator=EmulatorConfig(
            backend="retroarch",
            savestate_dir=tmp_path / "ra",
            spinlab_state_dir=tmp_path / "sl",
            ra_game_basename="Test",
        ),
        data_dir=tmp_path,
        rom_dir=None,
    )
    app = create_app(db, config=cfg)
    assert isinstance(app.state.tcp, RetroArchOrchestrator)
