"""Tests for .poke scenario file parser."""

import json
import pytest
from tests.integration.poke_parser import parse_poke


SIMPLE_SCENARIO = """\
# entrance_goal.poke — Level entrance then normal goal
settle: 30

1: game_mode=20 level_num=0x105
2: level_start=1
15: exit_mode=1 fanfare=1
"""


def test_parse_header_settle():
    result = parse_poke(SIMPLE_SCENARIO)
    assert result["settle_frames"] == 30


def test_parse_poke_count():
    result = parse_poke(SIMPLE_SCENARIO)
    assert len(result["pokes"]) == 5


def test_parse_frame_1_pokes():
    result = parse_poke(SIMPLE_SCENARIO)
    frame_1 = [p for p in result["pokes"] if p["frame"] == 1]
    assert len(frame_1) == 2
    addrs = {p["addr"] for p in frame_1}
    assert 0x0100 in addrs  # game_mode
    assert 0x13BF in addrs  # level_num


def test_parse_hex_value():
    result = parse_poke(SIMPLE_SCENARIO)
    level_poke = [p for p in result["pokes"] if p["addr"] == 0x13BF][0]
    assert level_poke["value"] == 0x105  # 261 decimal


def test_parse_decimal_value():
    result = parse_poke(SIMPLE_SCENARIO)
    gm_poke = [p for p in result["pokes"] if p["addr"] == 0x0100][0]
    assert gm_poke["value"] == 20


def test_parse_frame_15():
    result = parse_poke(SIMPLE_SCENARIO)
    frame_15 = [p for p in result["pokes"] if p["frame"] == 15]
    assert len(frame_15) == 2


def test_comments_and_blank_lines_ignored():
    scenario = "# just a comment\n\nsettle: 10\n\n# another comment\n1: game_mode=20\n"
    result = parse_poke(scenario)
    assert len(result["pokes"]) == 1


def test_unknown_address_raises():
    scenario = "settle: 10\n1: bogus_addr=42\n"
    with pytest.raises(ValueError, match="Unknown address name"):
        parse_poke(scenario)


def test_default_settle():
    scenario = "1: game_mode=20\n"
    result = parse_poke(scenario)
    assert result["settle_frames"] == 30  # default


def test_output_is_json_serializable():
    result = parse_poke(SIMPLE_SCENARIO)
    # Should not raise
    json.dumps(result)
