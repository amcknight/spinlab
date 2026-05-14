"""Tests for RetroArch backend config additions."""
from pathlib import Path

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


def test_retroarch_paths_parse(tmp_path):
    cfg_path = _write_config(tmp_path, _minimal_config({
        "emulator": {
            "retroarch_path": "C:/RetroArch-Win64/retroarch.exe",
            "savestate_dir": "C:/RetroArch-Win64/saves/states",
            "spinlab_state_dir": "data/spinlab_states",
        },
    }))
    cfg = AppConfig.from_yaml(cfg_path)
    assert cfg.emulator.retroarch_path == Path("C:/RetroArch-Win64/retroarch.exe")
    assert cfg.emulator.savestate_dir == Path("C:/RetroArch-Win64/saves/states")
    assert cfg.emulator.spinlab_state_dir == Path("data/spinlab_states")


def test_retroarch_paths_default_to_none_when_omitted(tmp_path):
    cfg_path = _write_config(tmp_path, _minimal_config({"emulator": {}}))
    cfg = AppConfig.from_yaml(cfg_path)
    assert cfg.emulator.retroarch_path is None
    assert cfg.emulator.savestate_dir is None
    assert cfg.emulator.spinlab_state_dir is None


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
emulator: {}
""")
    cfg = AppConfig.from_yaml(config_yaml)
    assert cfg.emulator.ra_core_path is None
