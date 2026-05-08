"""SMW WRAM address constants — re-export from spinlab.retroarch.addresses.

The Python source of truth for memory addresses lives at
spinlab.retroarch.addresses. This file exists as a Mesen-era compatibility
shim; under the RA harness it just re-exports the canonical values so that
poke_parser.py (and any other consumer of ADDR_MAP) reads them from one place.

Note: the keys here MUST match the names used in tests/integration/scenarios/
.poke files (e.g., 'game_mode', 'level_num') — those names are stable user
input, not implementation detail.
"""
from spinlab.retroarch import addresses as _a

ADDR_MAP: dict[str, int] = {
    "game_mode": _a.ADDR_GAME_MODE,
    "level_num": _a.ADDR_LEVEL_NUM,
    "room_num": _a.ADDR_ROOM_NUM,
    "level_start": _a.ADDR_LEVEL_START,
    "player_anim": _a.ADDR_PLAYER_ANIM,
    "exit_mode": _a.ADDR_EXIT_MODE,
    "io": _a.ADDR_IO,
    "fanfare": _a.ADDR_FANFARE,
    "boss_defeat": _a.ADDR_BOSS_DEFEAT,
    "midway": _a.ADDR_MIDWAY,
    "cp_entrance": _a.ADDR_CP_ENTRANCE,
}
