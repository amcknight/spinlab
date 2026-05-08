"""Tests for RetroArch backend config additions."""
from pathlib import Path

import pytest
import yaml

from spinlab.config import AppConfig


def _write_config(tmp_path: Path, doc: dict) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return p


def _minimal_config(extras: dict | None = None) -> dict:
    base: dict = {
        "data": {"dir": "/tmp/spinlab"},
    }
    if extras:
        for k, v in extras.items():
            base.setdefault(k, {})
            if isinstance(v, dict):
                base[k].update(v)
            else:
                base[k] = v
    return base


def test_default_backend_is_mesen_lua(tmp_path):
    """Empty emulator block → backend defaults to mesen-lua."""
    cfg_path = _write_config(tmp_path, _minimal_config())
    cfg = AppConfig.from_yaml(cfg_path)
    assert cfg.emulator.backend == "mesen-lua"


def test_backend_retroarch_explicit(tmp_path):
    cfg_path = _write_config(tmp_path, _minimal_config({"emulator": {"backend": "retroarch"}}))
    cfg = AppConfig.from_yaml(cfg_path)
    assert cfg.emulator.backend == "retroarch"


def test_unknown_backend_raises_value_error(tmp_path):
    cfg_path = _write_config(tmp_path, _minimal_config({"emulator": {"backend": "snes9x"}}))
    with pytest.raises(ValueError, match="backend"):
        AppConfig.from_yaml(cfg_path)


def test_retroarch_paths_parse(tmp_path):
    cfg_path = _write_config(tmp_path, _minimal_config({
        "emulator": {
            "backend": "retroarch",
            "retroarch_path": "C:/RetroArch-Win64/retroarch.exe",
            "savestate_dir": "C:/RetroArch-Win64/saves/states",
            "spinlab_state_dir": "data/spinlab_states",
            "ra_game_basename": "Toothpaste World",
        },
    }))
    cfg = AppConfig.from_yaml(cfg_path)
    assert cfg.emulator.retroarch_path == Path("C:/RetroArch-Win64/retroarch.exe")
    assert cfg.emulator.savestate_dir == Path("C:/RetroArch-Win64/saves/states")
    assert cfg.emulator.spinlab_state_dir == Path("data/spinlab_states")
    assert cfg.emulator.ra_game_basename == "Toothpaste World"


def test_retroarch_paths_default_to_none_when_omitted(tmp_path):
    cfg_path = _write_config(tmp_path, _minimal_config({"emulator": {"backend": "retroarch"}}))
    cfg = AppConfig.from_yaml(cfg_path)
    assert cfg.emulator.retroarch_path is None
    assert cfg.emulator.savestate_dir is None
    assert cfg.emulator.spinlab_state_dir is None
    assert cfg.emulator.ra_game_basename is None


def test_nci_port_default_55355(tmp_path):
    cfg_path = _write_config(tmp_path, _minimal_config())
    cfg = AppConfig.from_yaml(cfg_path)
    assert cfg.network.nci_port == 55355


def test_nci_port_override(tmp_path):
    cfg_path = _write_config(tmp_path, _minimal_config({"network": {"nci_port": 12345}}))
    cfg = AppConfig.from_yaml(cfg_path)
    assert cfg.network.nci_port == 12345


def test_ra_core_path_parsed_from_yaml(tmp_path):
    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text("""
data:
  dir: /tmp/data
emulator:
  backend: retroarch
  retroarch_path: C:/RetroArch-Win64/retroarch.exe
  ra_core_path: C:/RetroArch-Win64/cores/snes9x_libretro.dll
""")
    cfg = AppConfig.from_yaml(config_yaml)
    assert cfg.emulator.ra_core_path == Path("C:/RetroArch-Win64/cores/snes9x_libretro.dll")


def test_ra_core_path_defaults_to_none_when_absent(tmp_path):
    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text("""
data:
  dir: /tmp/data
emulator:
  backend: retroarch
""")
    cfg = AppConfig.from_yaml(config_yaml)
    assert cfg.emulator.ra_core_path is None


def test_old_mesen_keys_still_parse(tmp_path):
    """Existing Mesen-shaped configs must continue to parse without errors."""
    cfg_path = _write_config(tmp_path, _minimal_config({
        "emulator": {
            "path": "C:/path/to/Mesen.exe",
            "lua_script": "lua/spinlab.lua",
            "script_data_dir": "C:/Users/me/Mesen2/LuaScriptData/spinlab",
        },
        "network": {
            "port": 15482,
            "dashboard_port": 15483,
        },
    }))
    cfg = AppConfig.from_yaml(cfg_path)
    assert cfg.emulator.backend == "mesen-lua"
    assert cfg.emulator.path == Path("C:/path/to/Mesen.exe")
    assert cfg.emulator.lua_script == Path("lua/spinlab.lua")
    assert cfg.network.port == 15482
