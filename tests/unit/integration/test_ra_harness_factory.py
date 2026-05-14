"""Tests for ROM registry + path resolvers used by ra_harness_factory.

These tests must NOT require RetroArch installed — they exercise the
resolver/factory plumbing only. Real RA launch is mocked.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest


def test_resolve_rom_path_returns_path_when_rom_present(tmp_path):
    from tests.integration.conftest import _resolve_rom_path, ROM_REGISTRY

    rom_dir = tmp_path / "roms"
    rom_dir.mkdir()
    # The default key must exist in the registry; create a file matching its filename.
    filename = ROM_REGISTRY["default"]
    (rom_dir / filename).write_bytes(b"\x00")

    fake_config = {"rom": {"dir": str(rom_dir)}}
    with patch("tests.integration.conftest._load_config", return_value=fake_config):
        path = _resolve_rom_path("default")

    assert path == rom_dir / filename


def test_resolve_rom_path_raises_on_unknown_key():
    from tests.integration.conftest import _resolve_rom_path

    with pytest.raises(RuntimeError, match="unknown rom_key"):
        _resolve_rom_path("not_a_real_key")


def test_resolve_rom_path_raises_on_missing_rom_file(tmp_path):
    from tests.integration.conftest import _resolve_rom_path

    rom_dir = tmp_path / "roms"
    rom_dir.mkdir()
    # rom_dir exists but the registered filename isn't in it.
    fake_config = {"rom": {"dir": str(rom_dir)}}
    with patch("tests.integration.conftest._load_config", return_value=fake_config):
        with pytest.raises(RuntimeError, match="ROM file not found"):
            _resolve_rom_path("default")


def test_resolve_rom_path_raises_on_missing_rom_dir_in_config():
    from tests.integration.conftest import _resolve_rom_path

    with patch("tests.integration.conftest._load_config", return_value={}):
        with pytest.raises(RuntimeError, match="rom.dir not configured"):
            _resolve_rom_path("default")
