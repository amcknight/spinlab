"""Tests for MemorySnapshot — the per-frame view of SMW state."""
from __future__ import annotations

import pytest

from spinlab.retroarch.nci import NCIClient
from spinlab.retroarch.snapshot import MemorySnapshot, read_snapshot


def test_snapshot_dataclass_shape():
    snap = MemorySnapshot(
        game_mode=0x0E, level_num=0x05, room_num=0, level_start=1, player_anim=0,
        exit_mode=0, io_port=0, fanfare=0, boss_defeat=0, midway=0, cp_entrance=0,
    )
    assert snap.game_mode == 0x0E
    assert snap.level_num == 0x05


def test_read_snapshot_against_fake_server(fake_nci_server):
    """All 11 addresses are read; values returned in the dataclass."""
    expected = {
        0x0100: 0x0E, 0x13BF: 0x05, 0x010B: 0x00, 0x1935: 0x01,
        0x0071: 0x00, 0x0DD5: 0x00, 0x1DFB: 0x00, 0x0906: 0x00,
        0x13C6: 0x00, 0x13CE: 0x00, 0x1B403: 0x00,
    }
    for addr, val in expected.items():
        fake_nci_server.handle(
            f"READ_CORE_RAM {addr:x} 1",
            f"READ_CORE_RAM {addr:x} {val:02x}\n",
        )

    client = NCIClient(host=fake_nci_server.address[0], port=fake_nci_server.address[1])
    snap = read_snapshot(client)

    assert snap.game_mode == 0x0E
    assert snap.level_num == 0x05
    assert snap.level_start == 0x01
