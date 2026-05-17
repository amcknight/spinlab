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
    nci_port: int = 55355  # RetroArch Network Command Interface port


@dataclass
class EmulatorConfig:
    retroarch_path: Path | None = None
    ra_core_path: Path | None = None
    savestate_dir: Path | None = None
    spinlab_state_dir: Path | None = None
    ra_movie_dir: Path | None = None  # where RA writes movie files; None → discover via NCI
    ra_core_subdir: str | None = None  # subdir name RA uses under savestate_directory for movies
    # e.g. "Snes9x" for snes9x_libretro.dll — not derivable from the DLL stem automatically


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

        retroarch_path = emu.get("retroarch_path")
        ra_core_path = emu.get("ra_core_path")
        savestate_dir = emu.get("savestate_dir")
        spinlab_state_dir = emu.get("spinlab_state_dir")
        ra_movie_dir = emu.get("ra_movie_dir")
        ra_core_subdir = emu.get("ra_core_subdir")

        return cls(
            network=NetworkConfig(
                host=net.get("host", "127.0.0.1"),
                port=net.get("port", 15482),
                dashboard_port=net.get("dashboard_port", 15483),
                nci_port=net.get("nci_port", 55355),
            ),
            emulator=EmulatorConfig(
                retroarch_path=Path(retroarch_path) if retroarch_path else None,
                ra_core_path=Path(ra_core_path) if ra_core_path else None,
                savestate_dir=Path(savestate_dir) if savestate_dir else None,
                spinlab_state_dir=Path(spinlab_state_dir) if spinlab_state_dir else None,
                ra_movie_dir=Path(ra_movie_dir) if ra_movie_dir else None,
                ra_core_subdir=ra_core_subdir if ra_core_subdir else None,
            ),
            data_dir=Path(raw["data"]["dir"]),
            rom_dir=Path(rom_dir_str) if rom_dir_str else None,
            category=raw.get("game", {}).get("category", "any%"),
        )
