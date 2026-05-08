"""Verify the integration-test ADDR_MAP matches spinlab.retroarch.addresses."""

from spinlab.retroarch import addresses as ra_addr
from tests.integration.addresses import ADDR_MAP


def test_addr_map_keys_match_lua_keys_used_in_poke_files():
    """The .poke files reference these names — they must all be present."""
    expected_keys = {
        "game_mode", "level_num", "room_num", "level_start", "player_anim",
        "exit_mode", "io", "fanfare", "boss_defeat", "midway", "cp_entrance",
    }
    assert expected_keys.issubset(ADDR_MAP.keys())


def test_addr_map_values_match_spinlab_retroarch_addresses():
    assert ADDR_MAP["game_mode"] == ra_addr.ADDR_GAME_MODE
    assert ADDR_MAP["level_num"] == ra_addr.ADDR_LEVEL_NUM
    assert ADDR_MAP["room_num"] == ra_addr.ADDR_ROOM_NUM
    assert ADDR_MAP["level_start"] == ra_addr.ADDR_LEVEL_START
    assert ADDR_MAP["player_anim"] == ra_addr.ADDR_PLAYER_ANIM
    assert ADDR_MAP["exit_mode"] == ra_addr.ADDR_EXIT_MODE
    assert ADDR_MAP["io"] == ra_addr.ADDR_IO
    assert ADDR_MAP["fanfare"] == ra_addr.ADDR_FANFARE
    assert ADDR_MAP["boss_defeat"] == ra_addr.ADDR_BOSS_DEFEAT
    assert ADDR_MAP["midway"] == ra_addr.ADDR_MIDWAY
    assert ADDR_MAP["cp_entrance"] == ra_addr.ADDR_CP_ENTRANCE
