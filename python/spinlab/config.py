"""Typed configuration — parsed once at startup from YAML."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass
class NetworkConfig:
    host: str = "127.0.0.1"
    port: int = 15482
    dashboard_port: int = 15483


@dataclass
class EmulatorConfig:
    path: Path | None = None
    lua_script: Path | None = None


@dataclass
class AppConfig:
    network: NetworkConfig
    emulator: EmulatorConfig
    data_dir: Path
    rom_dir: Path | None
    category: str = "any%"

    @classmethod
    def from_yaml(cls, path: Path) -> "AppConfig":
        """Parse config.yaml into typed config. Crashes loud on missing required keys."""
        with path.open(encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

        net = raw.get("network", {})
        emu = raw.get("emulator", {})
        rom_dir_str = raw.get("rom", {}).get("dir")

        emu_path = emu.get("path")
        lua_script = emu.get("lua_script")

        return cls(
            network=NetworkConfig(
                host=net.get("host", "127.0.0.1"),
                port=net.get("port", 15482),
                dashboard_port=net.get("dashboard_port", 15483),
            ),
            emulator=EmulatorConfig(
                path=Path(emu_path) if emu_path else None,
                lua_script=Path(lua_script) if lua_script else None,
            ),
            data_dir=Path(raw["data"]["dir"]),
            rom_dir=Path(rom_dir_str) if rom_dir_str else None,
            category=raw.get("game", {}).get("category", "any%"),
        )
