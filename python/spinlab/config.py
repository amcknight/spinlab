"""Typed configuration — parsed once at startup from YAML."""
from __future__ import annotations

from dataclasses import dataclass, field
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
    script_data_dir: Path | None = None


# SNES controller buttons reserved for the in-emulator invalidation combo.
# L+Select chosen to avoid collision with in-game controls (Start/Select combos
# are common in SNES games; L is typically unused during normal gameplay).
DEFAULT_INVALIDATE_COMBO = ["L", "Select"]


@dataclass
class PracticeConfig:
    invalidate_combo: list[str] = field(default_factory=lambda: list(DEFAULT_INVALIDATE_COMBO))


@dataclass
class AppConfig:
    network: NetworkConfig
    emulator: EmulatorConfig
    data_dir: Path
    rom_dir: Path | None
    category: str = "any%"
    practice: PracticeConfig = field(default_factory=PracticeConfig)

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
        script_data_dir = emu.get("script_data_dir")

        practice_raw = raw.get("practice", {})
        practice_cfg = PracticeConfig(
            invalidate_combo=list(practice_raw.get("invalidate_combo", DEFAULT_INVALIDATE_COMBO)),
        )

        return cls(
            network=NetworkConfig(
                host=net.get("host", "127.0.0.1"),
                port=net.get("port", 15482),
                dashboard_port=net.get("dashboard_port", 15483),
            ),
            emulator=EmulatorConfig(
                path=Path(emu_path) if emu_path else None,
                lua_script=Path(lua_script) if lua_script else None,
                script_data_dir=Path(script_data_dir) if script_data_dir else None,
            ),
            data_dir=Path(raw["data"]["dir"]),
            rom_dir=Path(rom_dir_str) if rom_dir_str else None,
            category=raw.get("game", {}).get("category", "any%"),
            practice=practice_cfg,
        )
