"""Parse .poke scenario files into poke_scenario JSON-serializable dicts.

Format:
    # comment
    settle: 30

    1: game_mode=20 level_num=0x105
    2: level_start=1
    15: exit_mode=1 fanfare=1

Each line is  frame: name=value name=value ...
Address names are resolved via addresses.ADDR_MAP.
Values are decimal by default; hex with 0x prefix.
"""
from __future__ import annotations

from tests.integration.addresses import ADDR_MAP

DEFAULT_SETTLE = 30


def _parse_value(s: str) -> int:
    """Parse a decimal or hex integer string."""
    s = s.strip()
    if s.startswith("0x") or s.startswith("0X"):
        return int(s, 16)
    return int(s)


def parse_poke(text: str) -> dict:
    """Parse .poke file content into a poke_scenario dict.

    Returns:
        {"event": "poke_scenario", "settle_frames": int,
         "pokes": [{"frame": int, "addr": int, "value": int}, ...]}
    """
    settle_frames = DEFAULT_SETTLE
    pokes: list[dict] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()

        # Skip comments and blank lines
        if not line or line.startswith("#"):
            continue

        # Header directive: settle
        if line.startswith("settle:"):
            settle_frames = int(line.split(":", 1)[1].strip())
            continue

        # Frame line: "N: addr=val addr=val ..."
        if ":" not in line:
            continue

        frame_str, rest = line.split(":", 1)
        frame = int(frame_str.strip())

        for token in rest.strip().split():
            if "=" not in token:
                raise ValueError(f"Invalid poke token (missing =): {token!r}")
            name, val_str = token.split("=", 1)
            name = name.strip()
            if name not in ADDR_MAP:
                raise ValueError(f"Unknown address name: {name!r}")
            pokes.append({
                "frame": frame,
                "addr": ADDR_MAP[name],
                "value": _parse_value(val_str),
            })

    return {
        "event": "poke_scenario",
        "settle_frames": settle_frames,
        "pokes": pokes,
    }


def parse_poke_file(path: str) -> dict:
    """Read and parse a .poke file from disk."""
    with open(path) as f:
        return parse_poke(f.read())
