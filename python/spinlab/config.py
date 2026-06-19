"""Typed configuration — parsed once at startup from YAML."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class NetworkConfig:
    host: str = "127.0.0.1"
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
class GamepadConfig:
    """Physical-gamepad menu mapping. Read directly off the pad (pygame), not
    the SMW-emulated input. ``buttons`` maps a menu verb to a pygame button
    index; ``modifier`` is the button held to open the menu. IDs are
    controller/mode specific — discover them with ``spinlab gamepad-probe``.
    Disabled by default; with no pad connected the menu is simply inactive.
    """
    enabled: bool = False
    device_index: int = 0          # which joystick (pygame index)
    modifier: int | None = None    # button id that OPENS the menu (held)
    buttons: dict[str, int] = field(default_factory=dict)  # verb -> button id


# Practice Simulation Engine rollout count.
#
# Spec §10 proposed 20000; profiling sample_episode on a populated post-gate
# SamplerState measured ~18.6µs/call on Andrew's dev box (2026-06-01). At that
# rate a 20k×|segments| matrix column costs ~370ms, which is too chunky for
# tick-rate refresh. 10000 keeps a single column under ~200ms while still
# giving a tight SE on the mean (SE shrinks as 1/sqrt(N), so dropping 20k→10k
# only widens it by sqrt(2) ≈ 1.41× — well within the noise floor of the
# underlying EM-Suite draws). Operators on faster hardware can bump it via
# config.yaml without code changes.
DEFAULT_PRACTICE_ENGINE_ROLLOUTS = 10000


@dataclass
class PracticeEngineConfig:
    rollouts: int = DEFAULT_PRACTICE_ENGINE_ROLLOUTS  # Monte Carlo rollouts per matrix column. See spec §10.


@dataclass
class AppConfig:
    network: NetworkConfig
    emulator: EmulatorConfig
    practice_engine: PracticeEngineConfig
    data_dir: Path
    rom_dir: Path | None
    category: str = "any%"
    # Optional physical-gamepad menu; defaults to disabled so existing configs
    # and direct AppConfig(...) constructions need not specify it.
    gamepad: GamepadConfig = field(default_factory=GamepadConfig)

    @classmethod
    def from_yaml(cls, path: Path) -> "AppConfig":
        """Parse config.yaml into typed config. Crashes loud on missing required keys."""
        with path.open(encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

        net = raw.get("network", {})
        emu = raw.get("emulator", {})
        pe = raw.get("practice_engine", {})
        gp = raw.get("gamepad", {})
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
            practice_engine=PracticeEngineConfig(
                rollouts=pe.get("rollouts", DEFAULT_PRACTICE_ENGINE_ROLLOUTS),
            ),
            gamepad=GamepadConfig(
                enabled=bool(gp.get("enabled", False)),
                device_index=int(gp.get("device_index", 0)),
                modifier=int(gp["modifier"]) if gp.get("modifier") is not None else None,
                buttons=dict(gp.get("buttons", {})),
            ),
            data_dir=Path(raw["data"]["dir"]),
            rom_dir=Path(rom_dir_str) if rom_dir_str else None,
            category=raw.get("game", {}).get("category", "any%"),
        )
