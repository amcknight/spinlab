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
    """Read all 11 SMW state bytes via NCI and return a snapshot.

    Issues 11 separate READ_CORE_RAM calls. With NCIClient's persistent socket
    this stays under one frame at the spike-measured p50 RTT. If 60Hz polling
    hits measurable latency in production, batch into a contiguous range of low
    addresses (most fields cluster in $0000-$13FF) and read those in one call.
    """
    return MemorySnapshot(
        game_mode=client.read_ram(a.ADDR_GAME_MODE, 1)[0],
        level_num=client.read_ram(a.ADDR_LEVEL_NUM, 1)[0],
        room_num=client.read_ram(a.ADDR_ROOM_NUM, 1)[0],
        level_start=client.read_ram(a.ADDR_LEVEL_START, 1)[0],
        player_anim=client.read_ram(a.ADDR_PLAYER_ANIM, 1)[0],
        exit_mode=client.read_ram(a.ADDR_EXIT_MODE, 1)[0],
        io_port=client.read_ram(a.ADDR_IO, 1)[0],
        fanfare=client.read_ram(a.ADDR_FANFARE, 1)[0],
        boss_defeat=client.read_ram(a.ADDR_BOSS_DEFEAT, 1)[0],
        midway=client.read_ram(a.ADDR_MIDWAY, 1)[0],
        cp_entrance=client.read_ram(a.ADDR_CP_ENTRANCE, 1)[0],
    )
