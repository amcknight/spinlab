"""Unit tests for MovieRecorder against a fake NCI client + tmp filesystem."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from spinlab.retroarch.movie import MovieRecorder
from spinlab.retroarch.exceptions import NCIProtocolError


@dataclass
class FakeNCI:
    """Records calls; doesn't touch the network."""
    calls: list[str] = field(default_factory=list)
    status_responsive: bool = True

    def record_replay(self) -> None:
        self.calls.append("record_replay")

    def halt_replay(self) -> None:
        self.calls.append("halt_replay")

    def get_status(self):
        if not self.status_responsive:
            raise NCIProtocolError("simulated unresponsive RA")
        return type("S", (), {"state": "PLAYING"})()


def test_recorder_starts_idle():
    rec = MovieRecorder(client=FakeNCI(), movie_dir=Path("/tmp"))
    assert not rec.is_recording()


def test_start_toggles_record_and_marks_active(tmp_path):
    fake = FakeNCI()
    rec = MovieRecorder(client=fake, movie_dir=tmp_path)
    rec.start(tmp_path / "out.replay")
    assert fake.calls == ["record_replay"]
    assert rec.is_recording()


def test_stop_toggles_record_polls_for_file_then_renames(tmp_path):
    fake = FakeNCI()
    rec = MovieRecorder(client=fake, movie_dir=tmp_path, _poll_interval_s=0.01)
    dest = tmp_path / "out.replay"
    rec.start(dest)

    # Simulate RA writing a .replay file on halt — the recorder should find it
    # via iterdir baseline set-diff and move it to dest.
    ra_file = tmp_path / "the.replay2"
    ra_file.write_bytes(b"RPLY" + b"\x00" * 100)

    result = rec.stop()
    assert result == dest
    assert dest.exists()
    assert not ra_file.exists()
    assert not rec.is_recording()


def test_stop_raises_if_no_new_file_appears(tmp_path):
    fake = FakeNCI()
    rec = MovieRecorder(client=fake, movie_dir=tmp_path, _poll_interval_s=0.01, _poll_attempts=2)
    rec.start(tmp_path / "out.replay")
    with pytest.raises(FileNotFoundError):
        rec.stop()
    assert not rec.is_recording()


def test_stop_ignores_pre_existing_files(tmp_path):
    fake = FakeNCI()
    # An old .replay already in the dir — should NOT be picked up.
    old = tmp_path / "old.replay1"
    old.write_bytes(b"old content")
    rec = MovieRecorder(client=fake, movie_dir=tmp_path, _poll_interval_s=0.01, _poll_attempts=2)
    rec.start(tmp_path / "new.replay")
    with pytest.raises(FileNotFoundError):
        rec.stop()
    assert old.exists()  # old file untouched


def test_double_start_raises(tmp_path):
    rec = MovieRecorder(client=FakeNCI(), movie_dir=tmp_path)
    rec.start(tmp_path / "a.replay")
    with pytest.raises(RuntimeError, match="already recording"):
        rec.start(tmp_path / "b.replay")


def test_stop_without_start_raises(tmp_path):
    rec = MovieRecorder(client=FakeNCI(), movie_dir=tmp_path)
    with pytest.raises(RuntimeError, match="not recording"):
        rec.stop()
