"""Tests for build_orchestrator factory."""

import pytest

from spinlab.config import AppConfig, EmulatorConfig, NetworkConfig
from spinlab.retroarch.orchestrator import RetroArchOrchestrator, build_orchestrator


def _config(tmp_path, **emu_overrides) -> AppConfig:
    base = dict(
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


def test_build_orchestrator_rejects_missing_savestate_dir(tmp_path):
    cfg = _config(tmp_path, savestate_dir=None)
    with pytest.raises(ValueError, match="savestate_dir"):
        build_orchestrator(cfg)


def test_build_orchestrator_rejects_missing_spinlab_state_dir(tmp_path):
    cfg = _config(tmp_path, spinlab_state_dir=None)
    with pytest.raises(ValueError, match="spinlab_state_dir"):
        build_orchestrator(cfg)


def test_build_orchestrator_accepts_missing_ra_game_basename(tmp_path):
    """ra_game_basename is optional — RAClient overrides it from RA's
    GET_STATUS at connect() time.
    """
    cfg = _config(tmp_path, ra_game_basename=None)
    orch = build_orchestrator(cfg)
    assert orch is not None


# ---------------------------------------------------------------------------
# movie_dir resolution — three branches, no NCI required
# ---------------------------------------------------------------------------

def test_movie_dir_explicit_ra_movie_dir(tmp_path):
    """Branch 1: emu.ra_movie_dir set → RAClient gets that dir, movies enabled."""
    explicit = tmp_path / "custom_movies"
    cfg = _config(tmp_path, ra_movie_dir=explicit, ra_core_subdir="Snes9x")
    orch = build_orchestrator(cfg)
    assert orch._enable_movies is True
    assert orch._raclient._ra_movie_dir == explicit


def test_movie_dir_derived_from_savestate_and_core_subdir(tmp_path):
    """Branch 2: ra_movie_dir=None, savestate_dir + ra_core_subdir → derive."""
    cfg = _config(tmp_path, ra_movie_dir=None, ra_core_subdir="Snes9x")
    orch = build_orchestrator(cfg)
    expected = tmp_path / "ra" / "Snes9x"
    assert orch._enable_movies is True
    assert orch._raclient._ra_movie_dir == expected


def test_movie_dir_none_disables_movies(tmp_path):
    """Branch 3: neither ra_movie_dir nor ra_core_subdir → movies disabled."""
    cfg = _config(tmp_path, ra_movie_dir=None, ra_core_subdir=None)
    orch = build_orchestrator(cfg)
    assert orch._enable_movies is False
    assert orch._raclient._ra_movie_dir is None


def test_movie_dir_no_double_append_when_savestate_dir_already_includes_subdir(tmp_path):
    """Branch 2 corner: savestate_dir already ends with ra_core_subdir → don't double-append.

    RA users sometimes set savestate_dir to the per-core dir directly
    (e.g. C:\\RetroArch-Win64\\states\\Snes9x).
    """
    savestate_per_core = tmp_path / "ra" / "Snes9x"
    cfg = _config(
        tmp_path,
        savestate_dir=savestate_per_core,
        ra_movie_dir=None,
        ra_core_subdir="Snes9x",
    )
    orch = build_orchestrator(cfg)
    assert orch._enable_movies is True
    assert orch._raclient._ra_movie_dir == savestate_per_core
