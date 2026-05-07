"""Tests for MemorySnapshot — the per-frame view of SMW state."""
from spinlab.retroarch.nci import NCIClient
from spinlab.retroarch.snapshot import MemorySnapshot, read_snapshot


def test_snapshot_dataclass_shape():
    """All 11 fields are settable and readable; documents the frozen-dataclass contract."""
    snap = MemorySnapshot(
        game_mode=0x01, level_num=0x02, room_num=0x03, level_start=0x04,
        player_anim=0x05, exit_mode=0x06, io_port=0x07, fanfare=0x08,
        boss_defeat=0x09, midway=0x0A, cp_entrance=0x0B,
    )
    assert snap.game_mode == 0x01
    assert snap.level_num == 0x02
    assert snap.room_num == 0x03
    assert snap.level_start == 0x04
    assert snap.player_anim == 0x05
    assert snap.exit_mode == 0x06
    assert snap.io_port == 0x07
    assert snap.fanfare == 0x08
    assert snap.boss_defeat == 0x09
    assert snap.midway == 0x0A
    assert snap.cp_entrance == 0x0B


def test_read_snapshot_maps_each_address_to_its_field(fake_nci_server):
    """Every address must map to its correct field — distinct sentinels detect any swap."""
    # Pick a unique non-zero byte per address so a wrong address->field mapping
    # would surface as a wrong field value.
    addr_to_value = {
        0x0100: 0x11,  # game_mode
        0x13BF: 0x22,  # level_num
        0x010B: 0x33,  # room_num
        0x1935: 0x44,  # level_start
        0x0071: 0x55,  # player_anim
        0x0DD5: 0x66,  # exit_mode
        0x1DFB: 0x77,  # io_port
        0x0906: 0x88,  # fanfare
        0x13C6: 0x99,  # boss_defeat
        0x13CE: 0xAA,  # midway
        0x1B403: 0xBB,  # cp_entrance
    }
    for addr, val in addr_to_value.items():
        fake_nci_server.handle(
            f"READ_CORE_RAM {addr:x} 1",
            f"READ_CORE_RAM {addr:x} {val:02x}\n",
        )

    client = NCIClient(host=fake_nci_server.address[0], port=fake_nci_server.address[1])
    snap = read_snapshot(client)

    assert snap.game_mode == 0x11
    assert snap.level_num == 0x22
    assert snap.room_num == 0x33
    assert snap.level_start == 0x44
    assert snap.player_anim == 0x55
    assert snap.exit_mode == 0x66
    assert snap.io_port == 0x77
    assert snap.fanfare == 0x88
    assert snap.boss_defeat == 0x99
    assert snap.midway == 0xAA
    assert snap.cp_entrance == 0xBB
