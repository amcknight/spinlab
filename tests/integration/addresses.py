"""SNES memory address constants mirroring lua/spinlab.lua lines 43-53."""

ADDR_MAP: dict[str, int] = {
    "game_mode":    0x0100,
    "level_num":    0x13BF,
    "room_num":     0x010B,
    "level_start":  0x1935,
    "player_anim":  0x0071,
    "exit_mode":    0x0DD5,
    "io_port":      0x1DFB,
    "fanfare":      0x0906,
    "boss_defeat":  0x13C6,
    "midway":       0x13CE,
    "cp_entrance":  0x1B403,
}
