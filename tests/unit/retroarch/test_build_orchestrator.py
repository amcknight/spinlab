"""Tests for build_orchestrator factory."""
from pathlib import Path

import pytest

from spinlab.config import AppConfig, EmulatorConfig, NetworkConfig
from spinlab.retroarch.orchestrator import RetroArchOrchestrator, build_orchestrator


def _config(tmp_path, **emu_overrides) -> AppConfig:
    base = dict(
        backend="retroarch",
        savestate_dir=tmp_path / "ra",
        spinlab_state_dir=tmp_path / "sl",
        ra_game_basename="Test Game",
    )
    base.update(emu_overrides)
    return AppConfig(
        network=NetworkConfig(),
        emulator=EmulatorConfig(**base),
        data_dir=tmp_path / "data",
        rom_dir=None,
    )


def test_build_orchestrator_returns_orchestrator(tmp_path):
    cfg = _config(tmp_path)
    orch = build_orchestrator(cfg)
    assert isinstance(orch, RetroArchOrchestrator)


def test_build_orchestrator_rejects_mesen_backend(tmp_path):
    cfg = AppConfig(
        network=NetworkConfig(),
        emulator=EmulatorConfig(backend="mesen-lua"),
        data_dir=tmp_path,
        rom_dir=None,
    )
    with pytest.raises(ValueError, match="retroarch"):
        build_orchestrator(cfg)


def test_build_orchestrator_rejects_missing_savestate_dir(tmp_path):
    cfg = _config(tmp_path, savestate_dir=None)
    with pytest.raises(ValueError, match="savestate_dir"):
        build_orchestrator(cfg)


def test_build_orchestrator_rejects_missing_spinlab_state_dir(tmp_path):
    cfg = _config(tmp_path, spinlab_state_dir=None)
    with pytest.raises(ValueError, match="spinlab_state_dir"):
        build_orchestrator(cfg)


def test_build_orchestrator_accepts_missing_ra_game_basename(tmp_path):
    """ra_game_basename is intentionally optional — the orchestrator overrides
    it from RA's GET_STATUS at connect() time, so the config value (if any) is
    just a stale-or-fallback hint."""
    cfg = _config(tmp_path, ra_game_basename=None)
    orch = build_orchestrator(cfg)
    assert orch is not None  # no ValueError raised
