"""Pin SMW address constants — these are kaizosplits-derived and must not drift."""
from spinlab.retroarch import addresses as a


def test_smw_address_constants():
    # Memory map (must match lua/addresses.lua).
    assert a.ADDR_GAME_MODE == 0x0100
    assert a.ADDR_LEVEL_NUM == 0x13BF
    assert a.ADDR_ROOM_NUM == 0x010B
    assert a.ADDR_LEVEL_START == 0x1935
    assert a.ADDR_PLAYER_ANIM == 0x0071
    assert a.ADDR_EXIT_MODE == 0x0DD5
    assert a.ADDR_IO == 0x1DFB
    assert a.ADDR_FANFARE == 0x0906
    assert a.ADDR_BOSS_DEFEAT == 0x13C6
    assert a.ADDR_MIDWAY == 0x13CE
    assert a.ADDR_CP_ENTRANCE == 0x1B403


def test_smw_io_port_values():
    assert a.IO_ORB == 3
    assert a.IO_GOAL == 4
    assert a.IO_KEY == 7
    assert a.IO_FADEOUT == 8
