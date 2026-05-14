"""POST /api/emulator/launch behavior."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from spinlab.config import AppConfig, EmulatorConfig, NetworkConfig
from spinlab.dashboard import create_app
from spinlab.db import Database


def _app(
    tmp_path: Path,
    *,
    retroarch_path: Path | None = None,
    rom_dir: Path | None = None,
) -> TestClient:
    db = Database(":memory:")
    emu_kwargs: dict = dict(
        savestate_dir=tmp_path / "ra",
        spinlab_state_dir=tmp_path / "sl",
    )
    if retroarch_path is not None:
        emu_kwargs["retroarch_path"] = retroarch_path
    cfg = AppConfig(
        network=NetworkConfig(),
        emulator=EmulatorConfig(**emu_kwargs),
        data_dir=tmp_path,
        rom_dir=rom_dir,
    )
    return TestClient(create_app(db, config=cfg))


def test_retroarch_launch_400_when_retroarch_path_missing(tmp_path):
    """Under retroarch with no retroarch_path configured, 400 with helpful message."""
    rom_dir = tmp_path / "roms"
    rom_dir.mkdir()
    (rom_dir / "test.smc").write_bytes(b"\x00" * 1024)
    with patch("spinlab.routes.system._retroarch_already_running", return_value=False), \
         _app(tmp_path, retroarch_path=None, rom_dir=rom_dir) as client:
        resp = client.post("/api/emulator/launch", json={"rom": "test.smc"})
    assert resp.status_code == 400
    assert "retroarch_path" in resp.json()["detail"]


def test_retroarch_launch_returns_already_running_when_ra_alive(tmp_path):
    """If NCI ping succeeds, don't launch a second RA instance."""
    fake_ra = tmp_path / "ra.exe"
    fake_ra.write_bytes(b"")
    with patch("spinlab.routes.system._retroarch_already_running", return_value=True), \
         patch("spinlab.routes.system.subprocess.Popen") as mock_popen, \
         _app(tmp_path, retroarch_path=fake_ra) as client:
        resp = client.post("/api/emulator/launch", json={"rom": "x.smc"})
    assert resp.status_code == 200
    assert resp.json() == {"status": "already_running"}
    mock_popen.assert_not_called()


def test_retroarch_launch_spawns_with_rom_when_not_running(tmp_path):
    """RA not running, ROM exists → spawn retroarch.exe with the ROM."""
    fake_ra = tmp_path / "ra.exe"
    fake_ra.write_bytes(b"")
    rom_dir = tmp_path / "roms"
    rom_dir.mkdir()
    rom = rom_dir / "test.smc"
    rom.write_bytes(b"\x00" * 1024)

    with patch("spinlab.routes.system._retroarch_already_running", return_value=False), \
         patch("spinlab.routes.system.subprocess.Popen") as mock_popen, \
         _app(tmp_path, retroarch_path=fake_ra, rom_dir=rom_dir) as client:
        resp = client.post("/api/emulator/launch", json={"rom": "test.smc"})
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    mock_popen.assert_called_once()
    cmd = mock_popen.call_args[0][0]
    # cmd should be [retroarch_path, ..., rom_path]
    assert cmd[0] == str(fake_ra)
    assert cmd[-1] == str(rom.resolve())


def test_retroarch_launch_uses_libretro_core_when_present(tmp_path):
    """If <retroarch_dir>/cores/snes9x_libretro.dll exists, pass -L <core>."""
    ra_dir = tmp_path / "RetroArch"
    ra_dir.mkdir()
    fake_ra = ra_dir / "ra.exe"
    fake_ra.write_bytes(b"")
    cores_dir = ra_dir / "cores"
    cores_dir.mkdir()
    core = cores_dir / "snes9x_libretro.dll"
    core.write_bytes(b"")
    rom_dir = tmp_path / "roms"
    rom_dir.mkdir()
    rom = rom_dir / "test.smc"
    rom.write_bytes(b"\x00" * 1024)

    with patch("spinlab.routes.system._retroarch_already_running", return_value=False), \
         patch("spinlab.routes.system.subprocess.Popen") as mock_popen, \
         _app(tmp_path, retroarch_path=fake_ra, rom_dir=rom_dir) as client:
        resp = client.post("/api/emulator/launch", json={"rom": "test.smc"})
    assert resp.status_code == 200
    cmd = mock_popen.call_args[0][0]
    assert "-L" in cmd
    assert str(core) in cmd


def test_retroarch_launch_400_when_rom_missing_from_rom_dir(tmp_path):
    fake_ra = tmp_path / "ra.exe"
    fake_ra.write_bytes(b"")
    rom_dir = tmp_path / "roms"
    rom_dir.mkdir()
    with patch("spinlab.routes.system._retroarch_already_running", return_value=False), \
         _app(tmp_path, retroarch_path=fake_ra, rom_dir=rom_dir) as client:
        resp = client.post("/api/emulator/launch", json={"rom": "missing.smc"})
    assert resp.status_code == 400
    assert "ROM not found" in resp.json()["detail"]


def test_retroarch_launch_rejects_path_traversal(tmp_path):
    """ROM path outside rom_dir is rejected."""
    fake_ra = tmp_path / "ra.exe"
    fake_ra.write_bytes(b"")
    rom_dir = tmp_path / "roms"
    rom_dir.mkdir()
    with patch("spinlab.routes.system._retroarch_already_running", return_value=False), \
         _app(tmp_path, retroarch_path=fake_ra, rom_dir=rom_dir) as client:
        resp = client.post("/api/emulator/launch", json={"rom": "../escape.smc"})
    assert resp.status_code == 400
    assert "outside rom_dir" in resp.json()["detail"]
