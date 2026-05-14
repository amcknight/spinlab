"""Tests for MemorySnapshot — the per-frame view of SMW state."""
from spinlab.retroarch.nci import NCIClient
from spinlab.retroarch.snapshot import MemorySnapshot, read_snapshot


def test_read_snapshot_maps_each_address_to_its_field(fake_nci_server):
    """Every address must map to its correct field — distinct sentinels detect any swap."""
    # Pick a unique non-zero byte per address so a wrong address->field mapping
    # would surface as a wrong field value.
    addr_to_value = {
        0x0071: 0x55,  # player_anim
        0x0100: 0x11,  # game_mode
        0x010B: 0x33,  # room_num
        0x0906: 0x88,  # fanfare
        0x0DD5: 0x66,  # exit_mode
        0x13BF: 0x22,  # level_num
        0x13C6: 0x99,  # boss_defeat
        0x13CE: 0xAA,  # midway
        0x1935: 0x44,  # level_start
        0x1DFB: 0x77,  # io_port
        0x1B403: 0xBB, # cp_entrance
    }

    # read_snapshot now clusters reads into 6 contiguous ranges; build a reply
    # for each cluster, with each address's sentinel byte at its offset.
    def _cluster_reply(start: int, length: int) -> str:
        buf = bytearray(length)
        for addr, val in addr_to_value.items():
            if start <= addr < start + length:
                buf[addr - start] = val
        hex_bytes = " ".join(f"{b:02x}" for b in buf)
        return f"READ_CORE_RAM {start:x} {hex_bytes}\n"

    clusters = [
        (0x0071, 0x010B - 0x0071 + 1),  # player_anim, game_mode, room_num
        (0x0906, 0x0DD5 - 0x0906 + 1),  # fanfare, exit_mode
        (0x13BF, 0x13CE - 0x13BF + 1),  # level_num, boss_defeat, midway
        (0x1935, 1),                    # level_start
        (0x1DFB, 1),                    # io_port
        (0x1B403, 1),                   # cp_entrance
    ]
    for start, length in clusters:
        fake_nci_server.handle(
            f"READ_CORE_RAM {start:x} {length}",
            _cluster_reply(start, length),
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
