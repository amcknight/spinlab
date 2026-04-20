-- addresses.lua — Single source of truth for SNES memory addresses.
-- Both spinlab.lua and poke_engine.lua load this via dofile().
-- Tests parse this file at import time (tests/integration/addresses.py).
--
-- Ported from kaizosplits/Memory.cs

ADDR_GAME_MODE     = 0x0100   -- game mode: 18=prepare level, 20=in level
ADDR_LEVEL_NUM     = 0x13BF   -- current level number
ADDR_ROOM_NUM      = 0x010B   -- current room/sublevel
ADDR_LEVEL_START   = 0x1935   -- 0->1 when player appears in level
ADDR_PLAYER_ANIM   = 0x0071   -- player animation: 9=death
ADDR_EXIT_MODE     = 0x0DD5   -- 0=not exiting, non-zero=exiting level
ADDR_IO            = 0x1DFB   -- SPC I/O: 3=orb, 4=goal, 7=key, 8=fadeout
ADDR_FANFARE       = 0x0906   -- steps to 1 when goal reached
ADDR_BOSS_DEFEAT   = 0x13C6   -- 0=alive, non-zero=defeated
ADDR_MIDWAY        = 0x13CE   -- midway checkpoint tape: 0->1 when touched
ADDR_CP_ENTRANCE   = 0x1B403  -- ASM-style checkpoint entrance

-- SPC I/O port values (read from ADDR_IO / 0x1DFB)
IO_ORB     = 3   -- collected orb/dragon coin
IO_GOAL    = 4   -- normal goal tape/gate
IO_KEY     = 7   -- collected secret exit key
IO_FADEOUT = 8   -- screen fadeout (pipe/door exit)
