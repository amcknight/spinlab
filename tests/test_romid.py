"""Tests for ROM identity utilities."""
from pathlib import Path

from spinlab.romid import rom_checksum, game_name_from_filename


def test_rom_checksum_deterministic(tmp_path):
    rom = tmp_path / "test.sfc"
    rom.write_bytes(b"\x00" * 1024)
    c1 = rom_checksum(rom)
    c2 = rom_checksum(rom)
    assert c1 == c2
    assert len(c1) == 16
    assert all(ch in "0123456789abcdef" for ch in c1)


def test_rom_checksum_differs_for_different_content(tmp_path):
    rom_a = tmp_path / "a.sfc"
    rom_b = tmp_path / "b.sfc"
    rom_a.write_bytes(b"\x00" * 1024)
    rom_b.write_bytes(b"\xff" * 1024)
    assert rom_checksum(rom_a) != rom_checksum(rom_b)


def test_game_name_from_filename():
    assert game_name_from_filename("City of Dreams.sfc") == "City of Dreams"
    assert game_name_from_filename("My Hack v1.2.smc") == "My Hack v1.2"
    assert game_name_from_filename("noext") == "noext"
