"""Tests for .spinrec binary format."""
import struct
import pytest
from spinlab.spinrec import read_spinrec, write_spinrec, SpinrecHeader, MAGIC, VERSION


def make_spinrec(game_id: str = "abcdef0123456789", frames: list[int] | None = None) -> bytes:
    """Build a valid .spinrec binary blob."""
    frames = frames or [0, 0, 0]
    header = struct.pack("<4sH16sI6s", MAGIC, VERSION, game_id.encode("ascii"), len(frames), b"\x00" * 6)
    body = b"".join(struct.pack("<H", f) for f in frames)
    return header + body


class TestSpinrecRead:
    def test_reads_valid_file(self):
        data = make_spinrec(frames=[0x0000, 0x0011, 0x0FFF])
        header, frames = read_spinrec(data)
        assert header.magic == MAGIC
        assert header.version == VERSION
        assert header.game_id == "abcdef0123456789"
        assert header.frame_count == 3
        assert frames == [0x0000, 0x0011, 0x0FFF]

    def test_rejects_bad_magic(self):
        data = b"BAAD" + b"\x00" * 28
        with pytest.raises(ValueError, match="magic"):
            read_spinrec(data)

    def test_rejects_truncated_body(self):
        data = make_spinrec(frames=[1, 2, 3])
        truncated = data[:-2]  # chop last frame
        with pytest.raises(ValueError, match="truncated"):
            read_spinrec(truncated)

    def test_rejects_too_short_header(self):
        with pytest.raises(ValueError, match="header"):
            read_spinrec(b"\x00" * 10)


    def test_short_game_id_strips_nulls(self):
        data = make_spinrec(game_id="abc\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00", frames=[1])
        header, frames = read_spinrec(data)
        assert header.game_id == "abc"


class TestSpinrecWrite:
    def test_roundtrip(self):
        frames = [0x0001, 0x0010, 0x0100]
        data = write_spinrec("abcdef0123456789", frames)
        header, read_frames = read_spinrec(data)
        assert header.game_id == "abcdef0123456789"
        assert read_frames == frames

    def test_empty_frames(self):
        data = write_spinrec("abcdef0123456789", [])
        header, frames = read_spinrec(data)
        assert header.frame_count == 0
        assert frames == []
