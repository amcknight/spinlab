"""ADDR_MAP — string-keyed view of the production WRAM address constants.

The Python source of truth lives at ``spinlab.retroarch.addresses``. This
file exists because the ``.poke`` scenario format names addresses by
string (e.g. ``game_mode``, ``level_num``) — and those names are stable
user input that we don't want to inline as ASCII identifiers in
production code. We map names → integer addresses once, here, and the
poke parser + RA poke engine both consume this map.

Adding a new SMW address: add the constant to
``python/spinlab/retroarch/addresses.py`` and a string entry here.
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
    "controller_held": _a.ADDR_CONTROLLER_HELD,
    "controller_held_1": _a.ADDR_CONTROLLER_HELD_1,
}
