"""Unit tests for BSVRecorder against a fake NCI client + tmp filesystem."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from spinlab.retroarch.bsv import BSVRecorder
from spinlab.retroarch.exceptions import NCIProtocolError


@dataclass
class FakeNCI:
    """Records calls; doesn't touch the network."""
    calls: list[str] = field(default_factory=list)
    status_responsive: bool = True

    def bsv_record_toggle(self) -> None:
        self.calls.append("bsv_record_toggle")

    def get_status(self):
        if not self.status_responsive:
            raise NCIProtocolError("simulated unresponsive RA")
        return type("S", (), {"state": "PLAYING"})()


def test_recorder_starts_idle():
    rec = BSVRecorder(client=FakeNCI(), movie_dir=Path("/tmp"))
    assert not rec.is_recording()


def test_start_toggles_record_and_marks_active(tmp_path):
    fake = FakeNCI()
    rec = BSVRecorder(client=fake, movie_dir=tmp_path)
    rec.start(tmp_path / "out.bsv")
    assert fake.calls == ["bsv_record_toggle"]
    assert rec.is_recording()


def test_stop_toggles_record_polls_for_file_then_renames(tmp_path):
    fake = FakeNCI()
    rec = BSVRecorder(client=fake, movie_dir=tmp_path, _poll_interval_s=0.01)
    dest = tmp_path / "out.bsv"
    rec.start(dest)

    # Simulate RA writing a .bsv on toggle-off — the recorder should find it
    # via mtime baseline and move it to dest.
    ra_file = tmp_path / "RetroArch-auto.bsv"
    ra_file.write_bytes(b"BSV1" + b"\x00" * 100)

    result = rec.stop()
    assert result == dest
    assert dest.exists()
    assert not ra_file.exists()
    assert not rec.is_recording()


def test_stop_raises_if_no_new_bsv_appears(tmp_path):
    fake = FakeNCI()
    rec = BSVRecorder(client=fake, movie_dir=tmp_path, _poll_interval_s=0.01, _poll_attempts=2)
    rec.start(tmp_path / "out.bsv")
    with pytest.raises(FileNotFoundError):
        rec.stop()
    assert not rec.is_recording()


def test_stop_ignores_pre_existing_bsv_files(tmp_path):
    fake = FakeNCI()
    # An old .bsv already in the dir — should NOT be picked up.
    old = tmp_path / "old.bsv"
    old.write_bytes(b"old content")
    rec = BSVRecorder(client=fake, movie_dir=tmp_path, _poll_interval_s=0.01, _poll_attempts=2)
    rec.start(tmp_path / "new.bsv")
    with pytest.raises(FileNotFoundError):
        rec.stop()
    assert old.exists()  # old file untouched


def test_double_start_raises(tmp_path):
    rec = BSVRecorder(client=FakeNCI(), movie_dir=tmp_path)
    rec.start(tmp_path / "a.bsv")
    with pytest.raises(RuntimeError, match="already recording"):
        rec.start(tmp_path / "b.bsv")


def test_stop_without_start_raises(tmp_path):
    rec = BSVRecorder(client=FakeNCI(), movie_dir=tmp_path)
    with pytest.raises(RuntimeError, match="not recording"):
        rec.stop()
