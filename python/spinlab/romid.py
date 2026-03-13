"""ROM identity utilities: checksum and name extraction."""
from __future__ import annotations

import hashlib
from pathlib import Path


def rom_checksum(rom_path: Path) -> str:
    """Compute truncated SHA-256 of a ROM file. Returns 16 hex chars."""
    h = hashlib.sha256(rom_path.read_bytes())
    return h.hexdigest()[:16]


def game_name_from_filename(filename: str) -> str:
    """Extract display name from ROM filename (strip extension)."""
    p = Path(filename)
    if p.suffix.lower() in (".sfc", ".smc", ".fig", ".swc"):
        return p.stem
    return filename
