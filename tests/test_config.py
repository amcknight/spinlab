"""Tests for AppConfig loading."""
from pathlib import Path

import pytest
import yaml

from spinlab.config import AppConfig


class TestAppConfig:
    def test_from_yaml_minimal(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({
            "data": {"dir": "data"},
            "network": {"host": "127.0.0.1", "port": 15482, "dashboard_port": 15483},
        }))
        cfg = AppConfig.from_yaml(config_file)
        assert cfg.data_dir == Path("data")
        assert cfg.network.host == "127.0.0.1"
        assert cfg.network.port == 15482

    def test_from_yaml_full(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({
            "data": {"dir": "/data"},
            "network": {"host": "0.0.0.0", "port": 9999, "dashboard_port": 8080},
            "rom": {"dir": "/roms"},
            "emulator": {"path": "/emu", "lua_script": "script.lua"},
            "game": {"category": "100%"},
        }))
        cfg = AppConfig.from_yaml(config_file)
        assert cfg.rom_dir == Path("/roms")
        assert cfg.emulator.path == Path("/emu")
        assert cfg.category == "100%"

    def test_from_yaml_defaults(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({
            "data": {"dir": "data"},
        }))
        cfg = AppConfig.from_yaml(config_file)
        assert cfg.network.host == "127.0.0.1"
        assert cfg.network.port == 15482
        assert cfg.rom_dir is None
        assert cfg.category == "any%"

    def test_from_yaml_missing_data_dir_crashes(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({"network": {}}))
        with pytest.raises(KeyError):
            AppConfig.from_yaml(config_file)
