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
    # Controller 1 held buttons, byte 2 (A X L R). Read for the R-menu layer.
    # Defaulted because transition detection never consults it — existing
    # snapshot builders legitimately omit it; only read_snapshot + the menu
    # detector set it.
    controller_held: int = 0


def read_snapshot(client: NCIClient) -> MemorySnapshot:
    """Read all 12 SMW state bytes via NCI and return a snapshot.

    Clusters the 12 addresses into 7 contiguous-range READ_CORE_RAM calls
    instead of 12 individual reads. Each NCI read is a UDP round-trip, so
    cutting from 12 to 7 round-trips per snapshot is a measurable
    speedup for the 60Hz production poller and a ~30% speedup for each
    frame of the test harness's poke loop.

    Cluster boundaries are picked so each reply stays well under the 4096-byte
    UDP receive buffer (each WRAM byte serializes to ~3 chars of hex+space in
    RA's text protocol).

    Note: $17 (controller_held) is a lone read far below the low cluster
    ($0071+); including it in the low cluster would pull in ~90 bytes of
    unneeded WRAM between $17 and $0071.
    """
    # $17 is read FIRST so it acts as the synchronous barrier that ensures any
    # immediately-preceding WRITE_CORE_RAM 17 has been processed by RA before
    # the rest of the snapshot reads proceed.  In the poke engine, FRAMEADVANCE
    # is fire-and-forget: if it hasn't been processed yet when read_snapshot is
    # called, RA serialises it between NCI commands and the $17 NMI-write could
    # race with the later cluster reads.  Reading $17 first collapses that race:
    # RA must drain the write queue (FRAMEADVANCE + WRITE_CORE_RAM) before
    # replying, so by the time $17's reply arrives, the NMI has already run and
    # our re-write has already landed.
    controller_held = client.read_ram(a.ADDR_CONTROLLER_HELD, 1)[0]
    # $0071..$010B: player_anim, game_mode, room_num  (155 bytes)
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
        controller_held=controller_held,
    )
