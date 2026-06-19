"""MemorySnapshot — per-frame view of SMW state, built from NCI reads."""
from __future__ import annotations

from dataclasses import dataclass

from spinlab.retroarch import addresses as a
from spinlab.retroarch.nci import NCIClient


@dataclass(frozen=True)
class MemorySnapshot:
    """Frozen snapshot of every SMW byte that transition detection consults.

    All fields are single bytes — the address each one reads is fixed in
    ``addresses.py``.
    """

    game_mode: int
    level_num: int
    room_num: int
    level_start: int
    player_anim: int
    exit_mode: int
    io_port: int
    fanfare: int
    boss_defeat: int
    midway: int
    cp_entrance: int


def read_snapshot(client: NCIClient) -> MemorySnapshot:
    """Read 11 SMW state bytes via NCI into 6 contiguous-range READ_CORE_RAM calls.

    Clusters the 11 addresses into 6 reads instead of 11 individual reads.
    Each NCI read is a UDP round-trip, so clustering is a measurable speedup
    for the 60Hz production poller and a ~30% speedup for each frame of the
    test harness's poke loop.

    Cluster boundaries are picked so each reply stays well under the 4096-byte
    UDP receive buffer (each WRAM byte serializes to ~3 chars of hex+space in
    RA's text protocol).
    """
    # $0071..$010B read FIRST as the write-barrier: in the poke harness,
    # FRAMEADVANCE is fire-and-forget, so the first NCI read of the snapshot
    # forces RA to drain its write queue (FRAMEADVANCE + any WRITE_CORE_RAM)
    # before replying. Any read works as the barrier; this is just the first.
    c_low = client.read_ram(a.ADDR_PLAYER_ANIM, a.ADDR_ROOM_NUM - a.ADDR_PLAYER_ANIM + 1)
    # $0906..$0DD5: fanfare, exit_mode  (1232 bytes)
    c_mid = client.read_ram(a.ADDR_FANFARE, a.ADDR_EXIT_MODE - a.ADDR_FANFARE + 1)
    # $13BF..$13CE: level_num, boss_defeat, midway  (16 bytes)
    c_lv  = client.read_ram(a.ADDR_LEVEL_NUM, a.ADDR_MIDWAY - a.ADDR_LEVEL_NUM + 1)
    # Three lone bytes far enough apart that pulling the in-between ranges
    # would cost more than they save.
    level_start = client.read_ram(a.ADDR_LEVEL_START, 1)[0]
    io_port     = client.read_ram(a.ADDR_IO, 1)[0]
    cp_entrance = client.read_ram(a.ADDR_CP_ENTRANCE, 1)[0]

    return MemorySnapshot(
        player_anim=c_low[0],
        game_mode  =c_low[a.ADDR_GAME_MODE - a.ADDR_PLAYER_ANIM],
        room_num   =c_low[a.ADDR_ROOM_NUM - a.ADDR_PLAYER_ANIM],
        fanfare    =c_mid[0],
        exit_mode  =c_mid[a.ADDR_EXIT_MODE - a.ADDR_FANFARE],
        level_num  =c_lv[0],
        boss_defeat=c_lv[a.ADDR_BOSS_DEFEAT - a.ADDR_LEVEL_NUM],
        midway     =c_lv[a.ADDR_MIDWAY - a.ADDR_LEVEL_NUM],
        level_start=level_start,
        io_port    =io_port,
        cp_entrance=cp_entrance,
    )
