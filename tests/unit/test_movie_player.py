"""Unit tests for MoviePlayer against a fake NCI client + tmp filesystem."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from spinlab.retroarch.movie import MoviePlayer


@dataclass
class FakePlayerNCI:
    """Records calls; doesn't touch the network."""
    calls: list[str] = field(default_factory=list)

    def play_replay(self) -> None:
        self.calls.append("play_replay")

    def halt_replay(self) -> None:
        self.calls.append("halt_replay")

    def get_status(self):
        return type("S", (), {"state": "PLAYING"})()


def test_player_starts_idle(tmp_path):
    player = MoviePlayer(client=FakePlayerNCI(), movie_dir=tmp_path)
    assert not player.is_playing()


def test_play_copies_file_into_movie_dir_and_toggles(tmp_path):
    src = tmp_path / "source.replay"
    src.write_bytes(b"REPLAY1content")
    movie_dir = tmp_path / "movies"
    fake = FakePlayerNCI()
    player = MoviePlayer(client=fake, movie_dir=movie_dir)
    player.play(src)
    staged = movie_dir / "spinlab_movie.replay"
    assert staged.exists()
    assert staged.read_bytes() == b"REPLAY1content"
    assert fake.calls == ["play_replay"]
    assert player.is_playing()


def test_stop_toggles_off_and_unstages(tmp_path):
    src = tmp_path / "source.replay"
    src.write_bytes(b"x")
    movie_dir = tmp_path / "movies"
    fake = FakePlayerNCI()
    player = MoviePlayer(client=fake, movie_dir=movie_dir)
    player.play(src)
    player.stop()
    assert fake.calls == ["play_replay", "halt_replay"]
    assert not (movie_dir / "spinlab_movie.replay").exists()
    assert not player.is_playing()


def test_stop_is_idempotent_when_not_playing(tmp_path):
    player = MoviePlayer(client=FakePlayerNCI(), movie_dir=tmp_path)
    player.stop()  # no-op, no error


def test_play_raises_if_already_playing(tmp_path):
    src = tmp_path / "x.replay"
    src.write_bytes(b"x")
    player = MoviePlayer(client=FakePlayerNCI(), movie_dir=tmp_path)
    player.play(src)
    with pytest.raises(RuntimeError, match="already playing"):
        player.play(src)


def test_play_raises_if_source_missing(tmp_path):
    player = MoviePlayer(client=FakePlayerNCI(), movie_dir=tmp_path)
    with pytest.raises(FileNotFoundError):
        player.play(tmp_path / "doesnt_exist.replay")


def test_stop_clears_state_even_if_nci_raises(tmp_path):
    """If halt_replay raises, _is_playing must still be False afterwards."""
    src = tmp_path / "x.replay"
    src.write_bytes(b"x")

    class FailingFake(FakePlayerNCI):
        def halt_replay(self) -> None:
            super().halt_replay()
            raise RuntimeError("simulated NCI error")

    player = MoviePlayer(client=FailingFake(), movie_dir=tmp_path)
    player.play(src)
    with pytest.raises(RuntimeError, match="simulated NCI error"):
        player.stop()
    assert not player.is_playing()
    # Staged file should also have been cleaned up despite the raise
    assert not (tmp_path / "spinlab_movie.replay").exists()
