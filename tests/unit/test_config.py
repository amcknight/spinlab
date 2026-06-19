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
            "network": {"host": "127.0.0.1", "dashboard_port": 15483},
        }))
        cfg = AppConfig.from_yaml(config_file)
        assert cfg.data_dir == Path("data")
        assert cfg.network.host == "127.0.0.1"
        assert cfg.network.dashboard_port == 15483

    def test_from_yaml_full(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({
            "data": {"dir": "/data"},
            "network": {"host": "0.0.0.0", "dashboard_port": 8080},
            "rom": {"dir": "/roms"},
            "emulator": {"retroarch_path": "/retroarch.exe", "savestate_dir": "/states"},
            "game": {"category": "100%"},
        }))
        cfg = AppConfig.from_yaml(config_file)
        assert cfg.rom_dir == Path("/roms")
        assert cfg.emulator.retroarch_path == Path("/retroarch.exe")
        assert cfg.category == "100%"

    def test_from_yaml_defaults(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({
            "data": {"dir": "data"},
        }))
        cfg = AppConfig.from_yaml(config_file)
        assert cfg.network.host == "127.0.0.1"
        assert cfg.network.dashboard_port == 15483
        assert cfg.rom_dir is None
        assert cfg.category == "any%"

    def test_from_yaml_missing_data_dir_crashes(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({"network": {}}))
        with pytest.raises(KeyError):
            AppConfig.from_yaml(config_file)

    def test_gamepad_defaults_to_disabled(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({"data": {"dir": "data"}}))
        cfg = AppConfig.from_yaml(config_file)
        assert cfg.gamepad.enabled is False
        assert cfg.gamepad.device_index == 0
        assert cfg.gamepad.modifier is None
        assert cfg.gamepad.buttons == {}

    def test_gamepad_parses_full_section(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({
            "data": {"dir": "data"},
            "gamepad": {
                "enabled": True,
                "device_index": 1,
                "modifier": 8,
                "buttons": {
                    "pause": 9,
                    "toggle_science": 10,
                    "toggle_practice": 11,
                    "prev_segment": 4,
                    "next_segment": 5,
                },
            },
        }))
        cfg = AppConfig.from_yaml(config_file)
        assert cfg.gamepad.enabled is True
        assert cfg.gamepad.device_index == 1
        assert cfg.gamepad.modifier == 8
        assert cfg.gamepad.buttons["toggle_practice"] == 11


def test_ra_movie_dir_parsed_from_yaml(tmp_path):
    cfg_yaml = tmp_path / "config.yaml"
    cfg_yaml.write_text(
        "data:\n"
        "  dir: /tmp/data\n"
        "emulator:\n"
        "  backend: retroarch\n"
        "  ra_movie_dir: /custom/movies\n"
    )
    cfg = AppConfig.from_yaml(cfg_yaml)
    assert cfg.emulator.ra_movie_dir == Path("/custom/movies")


def test_ra_movie_dir_defaults_to_none():
    from spinlab.config import EmulatorConfig
    emu = EmulatorConfig()
    assert emu.ra_movie_dir is None
