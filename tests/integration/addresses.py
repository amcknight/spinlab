"""SNES memory address constants — parsed from lua/addresses.lua (single source of truth)."""
import re
from pathlib import Path

_LUA_FILE = Path(__file__).resolve().parents[2] / "lua" / "addresses.lua"

ADDR_MAP: dict[str, int] = {}

for line in _LUA_FILE.read_text().splitlines():
    m = re.match(r"(ADDR_\w+)\s*=\s*(0x[0-9a-fA-F]+)", line)
    if m:
        # Convert ADDR_GAME_MODE -> game_mode
        key = m.group(1).replace("ADDR_", "").lower()
        ADDR_MAP[key] = int(m.group(2), 16)
