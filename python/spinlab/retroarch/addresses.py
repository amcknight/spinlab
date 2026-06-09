"""SMW WRAM address constants — single source of truth.

Originally derived from kaizosplits/Memory.cs. Addresses are WRAM-flat
offsets (suitable for NCI's `READ_CORE_RAM <addr>`). For SMW these are
equivalent to SNES bus addresses minus 0x7E0000 for the $7E:0000-$7E:1FFF
range; the one exception is ADDR_CP_ENTRANCE which is at 0x1B403 (within
the $7F bank in WRAM-flat).
"""

# Game state.
ADDR_GAME_MODE = 0x0100  # game mode: 18=prepare level, 20=in level
ADDR_LEVEL_NUM = 0x13BF  # current level number
ADDR_ROOM_NUM = 0x010B  # current room/sublevel
ADDR_LEVEL_START = 0x1935  # 0->1 when player appears in level (entrance edge)
ADDR_PLAYER_ANIM = 0x0071  # player animation; 9 = death
# Controller 1 held buttons, byte 2 (A X L R - - - -). kaizosplits buttonsHeld2.
# Read for the R-menu command layer: R (0x10) arms the menu, X (0x40) is a
# command button. The newly-pressed twin ($18, buttonsPress2) is intentionally
# NOT read — the menu detector edge-detects the held byte instead (see
# retroarch/menu_detector.py).
ADDR_CONTROLLER_HELD = 0x17

# Exit / progression.
ADDR_EXIT_MODE = 0x0DD5  # 0 = not exiting; non-zero = exiting level
ADDR_IO = 0x1DFB  # SPC I/O port: see IO_* values below
ADDR_FANFARE = 0x0906  # steps to 1 when goal reached
ADDR_BOSS_DEFEAT = 0x13C6  # 0 = alive; non-zero = defeated
ADDR_MIDWAY = 0x13CE  # midway checkpoint tape: 0->1 on touch
ADDR_CP_ENTRANCE = 0x1B403  # ASM-style checkpoint entrance (kaizo hack patches)

# SPC I/O port values (read from ADDR_IO).
IO_ORB = 3
IO_GOAL = 4
IO_KEY = 7
IO_FADEOUT = 8
