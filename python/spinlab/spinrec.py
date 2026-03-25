"""Read/write .spinrec binary input recording format."""
from __future__ import annotations

import struct
from dataclasses import dataclass

MAGIC = b"SREC"
VERSION = 1
HEADER_SIZE = 32
HEADER_FMT = "<4sH16sI6s"


@dataclass
class SpinrecHeader:
    magic: bytes
    version: int
    game_id: str
    frame_count: int


def read_spinrec(data: bytes) -> tuple[SpinrecHeader, list[int]]:
    """Parse a .spinrec binary blob into header + frame list."""
    if len(data) < HEADER_SIZE:
        raise ValueError(f"Too short for header: {len(data)} bytes (need {HEADER_SIZE})")
    magic, version, game_id_bytes, frame_count, _ = struct.unpack_from(HEADER_FMT, data)
    if magic != MAGIC:
        raise ValueError(f"Bad magic: {magic!r} (expected {MAGIC!r})")
    game_id = game_id_bytes.rstrip(b"\x00").decode("ascii")
    expected_body = frame_count * 2
    actual_body = len(data) - HEADER_SIZE
    if actual_body < expected_body:
        raise ValueError(f"Body truncated: {actual_body} bytes (expected {expected_body})")
    frames = list(struct.unpack_from(f"<{frame_count}H", data, HEADER_SIZE))
    return SpinrecHeader(magic=magic, version=version, game_id=game_id, frame_count=frame_count), frames


def write_spinrec(game_id: str, frames: list[int]) -> bytes:
    """Build a .spinrec binary blob."""
    header = struct.pack(HEADER_FMT, MAGIC, VERSION, game_id.encode("ascii"), len(frames), b"\x00" * 6)
    body = struct.pack(f"<{len(frames)}H", *frames) if frames else b""
    return header + body
